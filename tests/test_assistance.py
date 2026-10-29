"""Knowledge discovery is shared across agents and gates expensive fallback without requiring a cold REA import."""

import json
import time

import anyio
import pytest

from decompnet import rea
from decompnet.app import agents, assistance, tools
from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    net = RomNetwork(tmp_path / "state.db", tmp_path / "home")
    agent, _ = net.local_agent("knowledge-tester")
    cfg = {"kind": "rom", "platform": "windows", "compiler": {"toolchain": "mingw-x86_64", "flags": []},
           "rom": {"program": "game.exe", "sha1": "original"}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,arch,root,config) VALUES(?,?,?,?,?)",
                         ("test", "Test", "x86_64", str(root), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,best_source,best_score,vram) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)", (pid, "tiny", 1, 1, b"\xc3", "[]", "ret", "int tiny(void) {}", 1, 0x401000))
    monkeypatch.setattr(net, "overview", lambda slug: {"kind": "rom", "title": "Test", "platform": "windows",
                                                      "compiler": {"toolchain": "mingw-x86_64"}})
    monkeypatch.setattr(net, "intake_line", lambda slug: "")
    return net, agent


@pytest.mark.parametrize("kwargs", [{}, {"fork": True}, {"analysis": True}])
def test_api_and_mcp_tools_have_identical_scoped_schemas(kwargs):
    specs = agents.api_tool_schemas(**kwargs)
    assert {s["function"]["name"] for s in specs} == set(tools.tool_names(**kwargs))
    full = {s["function"]["name"]: s["function"]["parameters"] for s in tools._tool_specs()}
    assert all(s["function"]["parameters"] == full[s["function"]["name"]] for s in specs)
    assert {"inspect_binary", "search_binary", "discover_assistance", "read_skill", "record_assistance"} <= set(full)
    specs[0]["function"]["parameters"].clear()
    assert agents.api_tool_schemas(**kwargs)[0]["function"]["parameters"]


def test_schema_discovery_also_works_inside_an_event_loop():
    tools._tool_specs.cache_clear()

    async def run():
        assert agents.api_tool_schemas(analysis=True)

    anyio.run(run)


def test_large_tool_evidence_is_bounded_without_trimming_task_instructions():
    messages = [{"role": "system", "content": "keep the user's scope"},
                {"role": "user", "content": "investigate this precise question"},
                {"role": "tool", "content": "Evidence ev_123\n" + "x" * 30000}]
    assert agents.fit(messages, 2000, trim_latest_tools=True) <= 2000
    assert messages[0]["content"] == "keep the user's scope"
    assert messages[1]["content"] == "investigate this precise question"
    assert messages[2]["content"].startswith("Evidence ev_123")
    assert "Incomplete tool preview" in messages[2]["content"]


def test_analysis_prompt_gets_analysis_skills(project, tmp_path, monkeypatch):
    net, _ = project
    monkeypatch.setattr(net, "overview", lambda slug: {"kind": "analysis", "platform": "rea", "title": "Test",
                                                      "compiler": {"toolchain": "rea"}})
    manager = agents.Manager(net, tmp_path, "http://localhost/mcp")
    prompt = manager.prompt(agents.Session("test", "api", "Test", "test", "agent", {}))
    assert tools._guide("analysis") in prompt and tools._guide("rea") in prompt
    assert tools._guide("gcc-kmc") not in prompt
    assert "discover_assistance" in prompt


def test_discovery_is_offline_until_connect_is_requested(project, monkeypatch):
    net, _ = project

    def unexpected(*args, **kwargs):
        pytest.fail("discovery started REA without connect=true")

    monkeypatch.setattr(rea, "Client", unexpected)
    result = json.loads(net.discover_assistance("test", "call graph", "tiny"))
    assert result["rea"]["status"] == "not_connected"
    assert {"workflow", "x86", "rea"} <= {s["id"] for s in result["skills"]}
    assert result["review"]["fingerprint"]
    with pytest.raises(NetworkError, match="booleans"):
        net.discover_assistance("test", connect="false")


def test_discovery_respects_explicitly_disabled_rea(project, monkeypatch):
    net, _ = project
    monkeypatch.setenv("DECOMP_REA", "off")
    result = json.loads(net.discover_assistance("test", function="tiny", connect=True))
    assert result["rea"]["status"] == "unavailable"
    assert "disabled" in result["rea"]["reason"]


def test_investigation_guidance_is_discoverable_and_readable_through_agent_tools(project, monkeypatch):
    net, agent = project
    monkeypatch.setenv("DECOMP_REA", "off")
    result = json.loads(tools.call(net, agent, "test", "discover_assistance",
                                   {"query": "investigate stalled matches and conflicting evidence"}))
    assert "investigation" in {s["id"] for s in result["skills"] if s["source"] == "bundled guide"}
    content = tools.call(net, agent, "test", "read_skill", {"skill": "investigation"})
    assert content == tools.call(net, agent, "test", "guide", {"topic": "investigation"})


def test_user_shared_skills_and_notes_are_discovered_within_the_reference_boundary(project, tmp_path):
    net, agent = project
    root = tmp_path / "skills"
    root.mkdir()
    skill = root / "SKILL.md"
    skill.write_text("---\nname: shader\ndescription: Recover shader pipelines\n---\nRead shader metadata first.\n")
    net.set_instructions("test", f"Use skills from {root}")
    net.add_note(agent, "test", "shader handles live in a shared table", "tiny")
    result = json.loads(net.discover_assistance("test", "shader", "tiny"))
    selected = next(s for s in result["skills"] if s["source"] == "user-shared SKILL.md")
    assert "Read shader metadata first" in net.read_skill("test", selected["id"])
    assert "shader handles" in result["relevant_notes"]
    with pytest.raises(NetworkError):
        net.read_skill("test", "reference:" + str(tmp_path / "private" / "SKILL.md"))
    outside = tmp_path / "outside.md"
    outside.write_text("not shared")
    link = root / "escape"
    try:
        link.symlink_to(outside)
    except OSError:
        return  # Windows may not allow symlinks for this test user.
    with pytest.raises(NetworkError):
        net.read_skill("test", "reference:" + str(link))


def test_fork_skills_are_explicitly_shared_and_room_hosts_cannot_authorize_local_reads(project, tmp_path, monkeypatch):
    net, _ = project
    root = tmp_path / "skills"
    root.mkdir()
    path = root / "SKILL.md"
    path.write_text("---\nname: fork-skill\ndescription: Project-specific guidance\n---\nRead the headers.")
    net.set_instructions("test", f"Skills: {root}")
    monkeypatch.setattr(net, "fork_slug", lambda slug: slug)
    assert "Read the headers" in net.read_skill("test", "reference:" + str(path))
    net.links["test"] = object()
    with pytest.raises(NetworkError, match="no user-shared"):
        net.read_skill("test", "reference:" + str(path))


class CatalogClient:
    def __init__(self):
        self.list_calls = 0

    def tools(self):
        self.list_calls += 1
        return [{"name": name, "description": "native call graph", "inputSchema": {"type": "object", "properties": {
            "procedure": {"type": "string"}}, "required": ["procedure"]}}
                for name in ("binary_session", "get_call_graph", "analyze_function", "open_binary", "install_adb_package")]

    def call(self, tool, arguments, **kwargs):
        assert tool == "binary_session"
        return {"result": {"tool_availability": {"get_call_graph": {"available": False, "reason": "provider lacks graph"}}}}


def test_catalog_uses_live_schemas_filters_mutating_tools_and_caches_availability():
    client = CatalogClient()
    result = rea.agent_catalog(client, rea.NATIVE_ALLOWED)
    assert {t["name"] for t in result["tools"]} == {"binary_session", "get_call_graph", "analyze_function"}
    graph = next(t for t in result["tools"] if t["name"] == "get_call_graph")
    assert graph["inputSchema"]["required"] == ["procedure"]
    assert graph["available"] is False and "provider lacks graph" in graph["availability"]["reason"]
    assert next(t for t in result["tools"] if t["name"] == "analyze_function")["available"] is None
    rea.agent_catalog(client, rea.NATIVE_ALLOWED)
    assert client.list_calls == 1
    rea.agent_catalog(client, rea.NATIVE_ALLOWED, refresh=True)
    assert client.list_calls == 2


def test_discovery_can_connect_to_the_matching_project_target(project, monkeypatch):
    net, _ = project
    client = CatalogClient()
    calls = []

    def connect(target, provider, **kwargs):
        calls.append((target, kwargs))
        return client

    monkeypatch.setattr(rea, "agent_client", connect)
    result = json.loads(net.discover_assistance("test", "call graph", "tiny", connect=True))
    assert result["rea"]["status"] == "connected"
    assert calls[0][1] == {"native": True, "connect": True}
    assert calls[0][0]["path"].endswith("game.exe")


def test_knowledge_review_gates_repeat_compiles_and_permutation_and_expires(project, monkeypatch):
    net, agent = project
    proj = net.project_row("test")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET attempts=2 WHERE project_id=?", (proj["id"],))
    monkeypatch.setattr(net, "compile_and_diff", lambda *args: "compiled")
    assert "Knowledge review needed" in tools.call(net, agent, "test", "compile_and_diff", {"function": "tiny", "source": "x"})
    assert not net.permuter.candidates(proj)
    with pytest.raises(NetworkError, match="first"):
        net.record_assistance(agent, "test", "tiny", "reviewed notes")
    net.discover_assistance("test", function="tiny")
    net.record_assistance(agent, "test", "tiny", "Compared the x86 guide and existing assembly")
    assert tools.call(net, agent, "test", "compile_and_diff", {"function": "tiny", "source": "x"}) == "compiled"
    assert not net.permuter.candidates(proj)
    net.record_assistance(agent, "test", "tiny", "Only register layout remains", "Relevant guidance exhausted; bounded mutations")
    assert [f["name"] for f in net.permuter.candidates(proj)] == ["tiny"]
    assert "Only register layout" in net.search_notes("test", "Knowledge review")
    with net.store.tx() as db:
        db.execute("INSERT INTO context_versions(project_id,version,header,created_at) VALUES(?,?,?,?)",
                   (proj["id"], 1, "new shared header", time.time()))
    assert not net.permuter.candidates(proj)
    assert not assistance.reviewed(net, proj, net._function("tiny", "test"))


def test_claims_include_an_automatic_assistance_packet(project, monkeypatch):
    net, agent = project
    proj, fn = net.project_row("test"), net._function("tiny", "test")
    with net.store.tx() as db:
        db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, time.time(), time.time() + 60))
    monkeypatch.setattr(net, "claim", lambda *args: "CLAIMED tiny")
    text = tools.call(net, agent, "test", "claim_function", {})
    assert "Relevant assistance" in text and '"not_connected"' in text
    assert json.loads(net._function("tiny", "test")["meta"])["assistance"]["fingerprint"] == assistance.fingerprint(net, proj)


def test_changed_function_boundaries_invalidate_a_review(project):
    net, agent = project
    proj = net.project_row("test")
    net.discover_assistance("test", function="tiny")
    net.record_assistance(agent, "test", "tiny", "Reviewed this function", "Only layout remains")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET target=?, size=2 WHERE project_id=?", (b"\x90\xc3", proj["id"]))
    assert not assistance.reviewed(net, proj, net._function("tiny", "test"))
    fresh = json.loads(net.discover_assistance("test", function="tiny"))
    assert "summary" not in fresh["review"] and "fallback_reason" not in fresh["review"]


@pytest.mark.parametrize("analysis_project", [False, True])
def test_rea_results_keep_evidence_and_do_not_allow_lifecycle_tools(project, monkeypatch, analysis_project):
    net, _ = project

    class EvidenceClient:
        def call(self, tool, args, **kwargs):
            assert kwargs["envelope"]
            return {"operation": tool, "evidence_id": "ev_123", "provider": {"name": "Ghidra"},
                    "confidence": "inferred", "limitations": ["indirect calls unknown"], "normalized_result": {"value": 7}}

    monkeypatch.setattr(rea, "agent_client", lambda *args, **kwargs: EvidenceClient())
    if analysis_project:
        proj = net.project_row("test")
        cfg = net.cfg(proj) | {"kind": "analysis", "target": {"path": str(proj["root"]) + "/game.exe"}}
        with net.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        monkeypatch.setattr(rea, "analysis_client", lambda *args, **kwargs: EvidenceClient())
    result = json.loads(net.agent_rea_tool("test", "analyze_function", {"procedure": "0x401000"}))
    assert result["evidence"]["evidence_id"] == "ev_123"
    assert result["evidence"]["limitations"] == ["indirect calls unknown"]
    assert result["result"] == {"value": 7}
    with pytest.raises(NetworkError, match="isn't available"):
        net.agent_rea_tool("test", "open_binary", {"path": "/another/target"})
