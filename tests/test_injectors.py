"""Turn-context injectors: policy parsing, the fail-loud execution contract,
and the full seam that proves a configured injector's output lands in a turn.

The classification rows mirror the automation-level ``inject:`` contract
(docs/ARCHITECTURE.md section 6, docs/INJECTORS.md):

- missing binary        -> InjectorError (loud)
- timeout               -> InjectorError (loud)
- non-zero exit         -> InjectorError (loud, stderr named)
- byte-exact INJECT_IDLE -> idle, no block
- bare-empty stdout     -> InjectorError (loud)
- anything else         -> the stdout becomes this turn's labeled block
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from drumbeat import injectors, packs, runner


def _write_tool(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def _injector(tool: Path, *, label: str = "test state") -> injectors.Injector:
    return injectors.Injector(
        argv=(str(tool),), label=label, apply_to=frozenset({"typing"})
    )


# ---- execution / classification ----


class TestInjectorClassification:
    def test_real_output_becomes_labeled_block(self, tmp_path: Path) -> None:
        tool = _write_tool(tmp_path, "state.sh", "printf 'line one\\nline two\\n'")
        outcome = injectors.run_injector(
            _injector(tool, label="Working set"), cwd=tmp_path, env=os.environ
        )
        assert outcome.idle is False
        assert outcome.text == "--- Working set ---\nline one\nline two"

    def test_forced_idle_sentinel_contributes_no_block(self, tmp_path: Path) -> None:
        tool = _write_tool(tmp_path, "idle.sh", "echo INJECT_IDLE")
        outcome = injectors.run_injector(_injector(tool), cwd=tmp_path, env=os.environ)
        assert outcome.idle is True
        assert outcome.text is None

    def test_sentinel_match_is_whole_stdout_not_prefix(self, tmp_path: Path) -> None:
        tool = _write_tool(tmp_path, "chatty.sh", "printf 'INJECT_IDLE but also this'")
        outcome = injectors.run_injector(_injector(tool), cwd=tmp_path, env=os.environ)
        assert outcome.idle is False
        assert outcome.text == "--- test state ---\nINJECT_IDLE but also this"

    def test_empty_stdout_refuses_loud(self, tmp_path: Path) -> None:
        tool = _write_tool(tmp_path, "empty.sh", "exit 0")
        with pytest.raises(injectors.InjectorError) as err:
            injectors.run_injector(_injector(tool), cwd=tmp_path, env=os.environ)
        assert "EMPTY stdout" in str(err.value)
        assert injectors.IDLE_SENTINEL in str(err.value)

    def test_whitespace_only_stdout_is_empty(self, tmp_path: Path) -> None:
        tool = _write_tool(tmp_path, "ws.sh", "printf '  \\n\\t\\n'")
        with pytest.raises(injectors.InjectorError) as err:
            injectors.run_injector(_injector(tool), cwd=tmp_path, env=os.environ)
        assert "EMPTY stdout" in str(err.value)

    def test_nonzero_exit_refuses_loud(self, tmp_path: Path) -> None:
        tool = _write_tool(tmp_path, "fail.sh", "echo 'real reason' >&2; exit 3")
        with pytest.raises(injectors.InjectorError) as err:
            injectors.run_injector(_injector(tool), cwd=tmp_path, env=os.environ)
        assert "exited 3" in str(err.value)
        assert "real reason" in str(err.value)

    def test_timeout_refuses_loud(self, tmp_path: Path) -> None:
        tool = _write_tool(tmp_path, "slow.sh", "sleep 5")
        with (
            mock.patch.object(injectors, "_INJECTOR_TIMEOUT_SECONDS", 0.2),
            pytest.raises(injectors.InjectorError) as err,
        ):
            injectors.run_injector(_injector(tool), cwd=tmp_path, env=os.environ)
        assert "timed out" in str(err.value)

    def test_missing_binary_refuses_loud(self, tmp_path: Path) -> None:
        ghost = injectors.Injector(
            argv=(str(tmp_path / "does-not-exist"),),
            label="ghost",
            apply_to=frozenset({"typing"}),
        )
        with pytest.raises(injectors.InjectorError) as err:
            injectors.run_injector(ghost, cwd=tmp_path, env=os.environ)
        assert "could not be executed" in str(err.value)

    def test_injector_runs_with_supplied_env_path(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _write_tool(bin_dir, "state-tool", "printf 'from the turn PATH'")
        inj = injectors.Injector(
            argv=("state-tool",), label="path check", apply_to=frozenset({"typing"})
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        outcome = injectors.run_injector(inj, cwd=tmp_path, env=env)
        assert outcome.text == "--- path check ---\nfrom the turn PATH"


# ---- policy parsing ----


class TestInjectorPolicy:
    def _write(self, workspace: Path, body: str) -> Path:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / injectors.POLICY_FILENAME).write_text(body, encoding="utf-8")
        return workspace

    def test_absent_file_is_empty_tuple(self, tmp_path: Path) -> None:
        assert injectors.load_policy(tmp_path) == ()

    def test_null_injectors_key_is_empty_tuple(self, tmp_path: Path) -> None:
        self._write(tmp_path, "injectors:\n")
        assert injectors.load_policy(tmp_path) == ()

    def test_valid_policy_parses(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "injectors:\n"
            '  - argv: ["context-tool", "--summarize"]\n'
            '    label: "Working context"\n'
            '    apply_to: ["typing", "chat"]\n',
        )
        (inj,) = injectors.load_policy(tmp_path)
        assert inj.argv == ("context-tool", "--summarize")
        assert inj.label == "Working context"
        assert inj.apply_to == frozenset({"typing", "chat"})

    def test_missing_label_fails_loud(self, tmp_path: Path) -> None:
        self._write(tmp_path, 'injectors:\n  - argv: ["t"]\n    apply_to: ["typing"]\n')
        with pytest.raises(injectors.InjectorPolicyError) as err:
            injectors.load_policy(tmp_path)
        assert "label" in str(err.value)

    def test_missing_argv_fails_loud(self, tmp_path: Path) -> None:
        self._write(tmp_path, 'injectors:\n  - label: "x"\n    apply_to: ["typing"]\n')
        with pytest.raises(injectors.InjectorPolicyError):
            injectors.load_policy(tmp_path)

    def test_empty_argv_fails_loud(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            'injectors:\n  - argv: []\n    label: "x"\n    apply_to: ["typing"]\n',
        )
        with pytest.raises(injectors.InjectorPolicyError):
            injectors.load_policy(tmp_path)

    def test_missing_apply_to_fails_loud(self, tmp_path: Path) -> None:
        self._write(tmp_path, 'injectors:\n  - argv: ["t"]\n    label: "x"\n')
        with pytest.raises(injectors.InjectorPolicyError) as err:
            injectors.load_policy(tmp_path)
        assert "apply_to" in str(err.value)

    def test_apply_to_is_open_vocabulary(self, tmp_path: Path) -> None:
        # Profile names are OPEN vocabulary (the owner picks them in
        # agent-config.yaml) -- so any non-empty string is accepted here; there
        # is no fixed set of interaction modes to validate against.
        self._write(
            tmp_path,
            'injectors:\n  - argv: ["t"]\n    label: "x"\n    apply_to: ["whatever-i-named-it"]\n',
        )
        loaded = injectors.load_policy(tmp_path)
        assert loaded[0].apply_to == frozenset({"whatever-i-named-it"})

    def test_apply_to_empty_string_fails_loud(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            'injectors:\n  - argv: ["t"]\n    label: "x"\n    apply_to: ["  "]\n',
        )
        with pytest.raises(injectors.InjectorPolicyError) as err:
            injectors.load_policy(tmp_path)
        assert "apply_to" in str(err.value)

    def test_unknown_key_fails_loud(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            'injectors:\n  - argv: ["t"]\n    label: "x"\n'
            '    apply_to: ["typing"]\n    timeout: 5\n',
        )
        with pytest.raises(injectors.InjectorPolicyError) as err:
            injectors.load_policy(tmp_path)
        assert "unknown key" in str(err.value)

    def test_non_list_injectors_fails_loud(self, tmp_path: Path) -> None:
        self._write(tmp_path, "injectors: yes\n")
        with pytest.raises(injectors.InjectorPolicyError):
            injectors.load_policy(tmp_path)

    def test_top_level_not_mapping_fails_loud(self, tmp_path: Path) -> None:
        self._write(tmp_path, "- just a list\n")
        with pytest.raises(injectors.InjectorPolicyError):
            injectors.load_policy(tmp_path)


# ---- collect_preamble (profile filtering) ----


class TestCollectPreamble:
    def _workspace(self, tmp_path: Path) -> Path:
        workspace = tmp_path / "workspace"
        _write_tool(workspace / "bin", "quick-tool", "printf 'quick context'")
        _write_tool(workspace / "bin", "chat-tool", "printf 'chat context'")
        (workspace / injectors.POLICY_FILENAME).write_text(
            "injectors:\n"
            '  - argv: ["quick-tool"]\n'
            '    label: "Quick"\n'
            '    apply_to: ["quick"]\n'
            '  - argv: ["chat-tool"]\n'
            '    label: "Chat"\n'
            '    apply_to: ["chat"]\n',
            encoding="utf-8",
        )
        return workspace

    def _env(self, workspace: Path, tmp_path: Path) -> dict[str, str]:
        return runner._turn_env(workspace, runs_dir=tmp_path / "runs")

    def test_none_profile_runs_nothing(self, tmp_path: Path) -> None:
        workspace = self._workspace(tmp_path)
        assert (
            injectors.collect_preamble(
                workspace, None, env=self._env(workspace, tmp_path)
            )
            == ()
        )

    def test_only_matching_profile_runs(self, tmp_path: Path) -> None:
        packs.reset_base_path_for_tests()
        workspace = self._workspace(tmp_path)
        blocks = injectors.collect_preamble(
            workspace, "quick", env=self._env(workspace, tmp_path)
        )
        assert blocks == ("--- Quick ---\nquick context",)

    def test_idle_injector_contributes_nothing(self, tmp_path: Path) -> None:
        packs.reset_base_path_for_tests()
        workspace = tmp_path / "workspace"
        _write_tool(workspace / "bin", "idle-tool", "echo INJECT_IDLE")
        (workspace / injectors.POLICY_FILENAME).write_text(
            'injectors:\n  - argv: ["idle-tool"]\n    label: "Q"\n'
            '    apply_to: ["quick"]\n',
            encoding="utf-8",
        )
        blocks = injectors.collect_preamble(
            workspace,
            "quick",
            env=runner._turn_env(workspace, runs_dir=tmp_path / "r"),
        )
        assert blocks == ()


# ---- full seam: a configured injector's output lands in the assembled turn ----


class TestInjectorSeam:
    def test_configured_injector_output_lands_in_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE PROOF. A workspace configures an injector; when a turn runs, the
        injector's labeled output is present in the text sent to the agent, and
        the person's own message is still last."""
        packs.reset_base_path_for_tests()
        workspace = tmp_path / "workspace"
        runs = tmp_path / "runs"
        runs.mkdir()
        _write_tool(workspace / "bin", "context-tool", "printf 'FLEET IS NOMINAL'")
        (workspace / injectors.POLICY_FILENAME).write_text(
            'injectors:\n  - argv: ["context-tool"]\n'
            '    label: "Working context"\n    apply_to: ["typing"]\n',
            encoding="utf-8",
        )

        # Collect exactly as turns._execute does, with the real turn env.
        blocks = injectors.collect_preamble(
            workspace, "typing", env=runner._turn_env(workspace, runs_dir=runs)
        )
        assert blocks == ("--- Working context ---\nFLEET IS NOMINAL",)

        captured: list[dict] = []

        def fake_invoke(cmd, *, cwd, timeout, on_progress=None, env=None):
            captured.append({"cmd": list(cmd), "env": dict(env or {})})
            return (0, json.dumps({"reply": "ok", "metadata": {}}), "", False)

        monkeypatch.setattr(runner, "_invoke_turn", fake_invoke)
        runner.resume_turn(
            "sess-injected",
            "what's the fleet status?",
            cwd=workspace,
            runs_dir=runs,
            preamble_blocks=blocks,
        )

        assert len(captured) == 1
        turn_text = captured[0]["cmd"][-1]
        # The injector's label AND its output are in the assembled turn...
        assert "--- Working context ---" in turn_text
        assert "FLEET IS NOMINAL" in turn_text
        # ...above the person's own message, which stays last (most salient).
        assert turn_text.index("FLEET IS NOMINAL") < turn_text.index(
            "what's the fleet status?"
        )

    def test_turn_without_injectors_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        packs.reset_base_path_for_tests()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        runs = tmp_path / "runs"
        runs.mkdir()

        captured: list[dict] = []

        def fake_invoke(cmd, *, cwd, timeout, on_progress=None, env=None):
            captured.append({"cmd": list(cmd), "env": dict(env or {})})
            return (0, json.dumps({"reply": "ok", "metadata": {}}), "", False)

        monkeypatch.setattr(runner, "_invoke_turn", fake_invoke)
        runner.resume_turn(
            "sess-plain",
            "hello",
            cwd=workspace,
            runs_dir=runs,
            preamble_blocks=(),
        )
        assert len(captured) == 1
        turn_text = captured[0]["cmd"][-1]
        # No block fence leaked into the turn; PATH is exactly the pack turn path.
        assert "--- " not in turn_text
        assert captured[0]["env"]["PATH"] == packs.turn_path(workspace)
