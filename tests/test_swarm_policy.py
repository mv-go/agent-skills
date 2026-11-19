"""Deterministic instruction-policy checks; these do not enforce runtime limits."""

from __future__ import annotations

import json
import re
import unittest
from collections import Counter
from pathlib import Path


SKILL = Path(__file__).parents[1] / "skills/budget-aware-swarm/SKILL.md"
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
RUNTIME_EFFORTS = {
    "sol": {"low", "medium", "high", "xhigh", "max"},
    "terra": set(EFFORTS),
    "luna": {"low", "medium", "high", "xhigh", "max"},
    "astra": set(EFFORTS),
}


def policy_from_skill() -> tuple[dict, str]:
    text = SKILL.read_text()
    match = re.search(r"```json\s+SWARM_POLICY_V2\s*\n(.*?)\n```", text, re.S)
    if not match:
        raise AssertionError("SKILL.md must contain the SWARM_POLICY_V2 instruction matrix")
    return json.loads(match.group(1)), text


def edge_for(policy: dict, parent_role: str, child_model: str) -> dict | None:
    return next((edge for edge in policy["edges"]
                 if edge["parent"] == parent_role and edge["child"] == child_model), None)


def may_spawn_effort(policy: dict, parent_role: str, child_model: str,
                     effort: str, runtime_supported: dict[str, set[str]]) -> bool:
    """Test-only decision helper driven by the skill matrix and supplied runtime catalog."""
    edge = edge_for(policy, parent_role, child_model)
    if edge is None or effort not in runtime_supported.get(child_model, set()):
        return False
    constraint = edge["efforts"]
    return constraint == "runtime-supported" or effort in constraint


def root_selection_allowed(policy: dict, model: str, effort: str,
                           runtime_supported: set[str]) -> bool:
    """Root family is fixed by the instruction; effort must exist in this runtime."""
    return model in {policy["models"][role]["model"] for role in ("astra_root", "sol_root")} and effort in runtime_supported


class OpenTreeModel:
    """Small TEST-ONLY simulator for open slots; never called by the skill/runtime."""

    def __init__(self, policy: dict):
        self.policy = policy
        self.total_open = 1  # root
        self.children: dict[str, list[tuple[str, str]]] = {"root": []}
        self.roles = {"root": "sol_root"}
        self.status: dict[str, str] = {"root": "open"}
        self.edge_counts: Counter[tuple[str, str]] = Counter()
        self.next_id = 0
        self.completed = 0

    def attempt(self, parent_id: str, child_model: str, effort: str,
                runtime_supported: dict[str, set[str]]) -> tuple[bool, str, str | None]:
        role = self.roles[parent_id]
        edge = edge_for(self.policy, role, child_model)
        if not may_spawn_effort(self.policy, role, child_model, effort, runtime_supported):
            return False, "edge-or-effort", None
        key = (parent_id, child_model)
        while self.edge_counts[key] >= edge["max_open_children"]:
            completed = self._eligible_completed(parent_id, child_model)
            if completed is None:
                return False, "parent-model-cap", None
            self._reclaim(completed)
        while self.total_open >= self.policy["global_open_threads_including_root"]:
            completed = self._eligible_completed()
            if completed is None:
                return False, "global-pool-full", None
            self._reclaim(completed)
        child_id = f"thread-{self.next_id}"
        self.next_id += 1
        self.children.setdefault(parent_id, []).append((child_model, child_id))
        child_role = "sol_child" if child_model == "sol" else child_model
        self.roles[child_id] = child_role
        self.children.setdefault(child_id, [])
        self.status[child_id] = "open"
        self.edge_counts[key] += 1
        self.total_open += 1
        return True, "spawned", child_id

    def mark_completed(self, thread_id: str) -> None:
        if self.status.get(thread_id) != "open":
            raise AssertionError(f"unknown or already completed thread: {thread_id}")
        self.status[thread_id] = "completed"
        self.completed += 1

    def _eligible_completed(self, parent_id: str | None = None,
                            child_model: str | None = None) -> str | None:
        parents = [parent_id] if parent_id is not None else list(self.children)
        for candidate_parent in parents:
            for model, thread_id in self.children.get(candidate_parent, []):
                if child_model is not None and model != child_model:
                    continue
                if self.status.get(thread_id) == "completed" and not self.children.get(thread_id):
                    return thread_id
        return None

    def _reclaim(self, thread_id: str) -> None:
        if self.status.get(thread_id) != "completed" or self.children.get(thread_id):
            raise AssertionError(f"thread is not an eligible completed resident: {thread_id}")
        for parent_id, items in self.children.items():
            for index, (model, child_id) in enumerate(items):
                if child_id == thread_id:
                    items.pop(index)
                    self.edge_counts[(parent_id, model)] -= 1
                    self.total_open -= 1
                    del self.status[thread_id]
                    del self.roles[thread_id]
                    del self.children[thread_id]
                    return
        raise AssertionError(f"unknown resident thread: {thread_id}")


class InstructionPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy, cls.skill = policy_from_skill()

    def test_complete_allowed_and_forbidden_edge_matrix(self):
        expected = {
            ("astra_root", "sol"): (1, ["low"]),
            ("astra_root", "terra"): (5, ["low", "medium", "high", "xhigh"]),
            ("astra_root", "luna"): (20, "runtime-supported"),
            ("sol_root", "sol"): (3, ["low"]),
            ("sol_root", "terra"): (5, ["low", "medium", "high", "xhigh"]),
            ("sol_root", "luna"): (20, "runtime-supported"),
            ("sol_child", "sol"): (3, ["low"]),
            ("sol_child", "terra"): (3, ["low", "medium", "high", "xhigh"]),
            ("sol_child", "luna"): (10, "runtime-supported"),
            ("terra", "luna"): (10, "runtime-supported"),
        }
        actual = {
            (edge["parent"], edge["child"]): (edge["max_open_children"], edge["efforts"])
            for edge in self.policy["edges"]
        }
        self.assertEqual(expected, actual)

        # Child families and creator roles are distinct sets: Sol child edges are checked.
        child_models = {"sol", "terra", "luna", "astra", "unknown"}
        parent_roles = {"astra_root", "sol_root", "sol_child", "terra", "luna", "astra", "unknown"}
        for parent in parent_roles:
            for child in child_models:
                self.assertEqual((parent, child) in expected,
                                 edge_for(self.policy, parent, child) is not None,
                                 f"unexpected or missing edge {parent} -> {child}")
        self.assertTrue(self.policy["models"]["luna"]["leaf"])

    def test_effort_decisions_use_matrix_and_runtime_catalog(self):
        for edge in self.policy["edges"]:
            for effort in EFFORTS:
                supported = effort in RUNTIME_EFFORTS[edge["child"]]
                constraint = edge["efforts"]
                expected = supported and (constraint == "runtime-supported" or effort in constraint)
                self.assertEqual(expected,
                                 may_spawn_effort(self.policy, edge["parent"], edge["child"],
                                                  effort, RUNTIME_EFFORTS),
                                 f"effort {effort} for {edge['parent']} -> {edge['child']}")
        self.assertTrue(may_spawn_effort(self.policy, "sol_root", "luna", "max", RUNTIME_EFFORTS))
        self.assertFalse(may_spawn_effort(self.policy, "sol_root", "luna", "ultra", RUNTIME_EFFORTS))
        self.assertFalse(may_spawn_effort(self.policy, "sol_child", "sol", "medium", RUNTIME_EFFORTS))
        self.assertFalse(may_spawn_effort(self.policy, "sol_child", "terra", "max", RUNTIME_EFFORTS))

    def test_root_model_effort_evidence_and_pause_gates(self):
        root = self.policy["models"]["sol_root"]
        self.assertEqual(root["model"], "Sol")
        self.assertEqual(root["root_effort_guidance"],
                         ["usually-high", "xhigh-when-needed", "max-deliberate-exceptionally-difficult"])
        self.assertTrue(root_selection_allowed(self.policy, "Sol", "low", {"low", "high"}))
        self.assertEqual(self.policy["models"]["astra_root"]["task_sol_dispatch_cap"], 1)
        self.assertTrue(root_selection_allowed(self.policy, "Astra", "high", {"high"}))
        self.assertFalse(root_selection_allowed(self.policy, "unknown", "high", {"high"}))
        self.assertFalse(root_selection_allowed(self.policy, "Sol", "ultra", {"high"}))
        normalized = " ".join(self.skill.lower().split())
        self.assertIn("the verified root is outside the sol and astra families", normalized)
        self.assertIn("use high usually", normalized)
        self.assertIn("xhigh when needed", normalized)
        self.assertIn("max only as a deliberate choice", normalized)
        self.assertIn("pause and ask the user", normalized)
        self.assertIn("selected effort is not supported by the active runtime", normalized)
        self.assertIn("do not automatically switch the root model", normalized)
        self.assertIn("requested", normalized)
        self.assertIn("effective", normalized)
        self.assertIn("unverified", normalized)

    def test_each_permitted_edge_fills_to_cap_then_refuses_next(self):
        for edge in self.policy["edges"]:
            with self.subTest(parent=edge["parent"], child=edge["child"]):
                tree = OpenTreeModel(self.policy)
                if edge["parent"] == "astra_root":
                    tree.roles["root"] = "astra_root"
                # Create a parent node for nested edge tests when the edge creator is Sol/Terra.
                if edge["parent"] == "sol_child":
                    _, _, parent_id = tree.attempt("root", "sol", "low", RUNTIME_EFFORTS)
                elif edge["parent"] == "terra":
                    _, _, parent_id = tree.attempt("root", "terra", "low", RUNTIME_EFFORTS)
                else:
                    parent_id = "root"
                self.assertIsNotNone(parent_id)
                parent_id = parent_id or "root"
                # The first suitable parent's own edge capacity is independent of other nodes.
                created = []
                for _ in range(edge["max_open_children"]):
                    ok, reason, thread_id = tree.attempt(parent_id, edge["child"],
                                                        "low" if edge["child"] != "luna" else "max",
                                                        RUNTIME_EFFORTS)
                    self.assertTrue(ok, reason)
                    created.append(thread_id)
                ok, reason, _ = tree.attempt(parent_id, edge["child"],
                                             "low" if edge["child"] != "luna" else "max",
                                             RUNTIME_EFFORTS)
                self.assertFalse(ok)
                self.assertEqual(reason, "parent-model-cap")

    def test_identical_roles_keep_independent_parent_counters(self):
        tree = OpenTreeModel(self.policy)
        parents = []
        for _ in range(2):
            ok, _, thread_id = tree.attempt("root", "sol", "low", RUNTIME_EFFORTS)
            self.assertTrue(ok)
            parents.append(thread_id)
        child_cap = edge_for(self.policy, "sol_child", "terra")["max_open_children"]
        for parent_id in parents:
            for _ in range(child_cap):
                self.assertTrue(tree.attempt(parent_id, "terra", "xhigh", RUNTIME_EFFORTS)[0])
            self.assertEqual(tree.attempt(parent_id, "terra", "xhigh", RUNTIME_EFFORTS)[1],
                             "parent-model-cap")

    def test_lifetime_spawn_count_is_unlimited_with_wait_completion_and_reuse(self):
        tree = OpenTreeModel(self.policy)
        active = []
        lifetime_created = 0
        parent_cap = edge_for(self.policy, "sol_root", "luna")["max_open_children"]
        lifetime_goal = self.policy["global_open_threads_including_root"] + 20
        for _ in range(lifetime_goal):
            if len(active) == parent_cap:
                ok, reason, _ = tree.attempt("root", "luna", "max", RUNTIME_EFFORTS)
                self.assertFalse(ok)
                self.assertEqual(reason, "parent-model-cap")
                # Wait observes completion, but this resident still counts until a later spawn reclaims it.
                completed_id = active.pop(0)
                tree.mark_completed(completed_id)
                self.assertEqual(tree.total_open, parent_cap + 1)
                self.assertEqual(tree.edge_counts[("root", "luna")], parent_cap)
            ok, reason, thread_id = tree.attempt("root", "luna", "max", RUNTIME_EFFORTS)
            self.assertTrue(ok, reason)
            active.append(thread_id)
            lifetime_created += 1
        self.assertGreater(lifetime_created, self.policy["global_open_threads_including_root"])
        self.assertEqual(len(active), parent_cap)
        self.assertEqual(tree.total_open, parent_cap + 1)

    def test_full_pool_refuses_then_wait_completion_retry_reuses_slot(self):
        tree = OpenTreeModel(self.policy)
        sol_parents = []
        root_sol_cap = edge_for(self.policy, "sol_root", "sol")["max_open_children"]
        root_luna_cap = edge_for(self.policy, "sol_root", "luna")["max_open_children"]
        per_sol_luna_cap = edge_for(self.policy, "sol_child", "luna")["max_open_children"]
        for _ in range(root_sol_cap):
            ok, reason, thread_id = tree.attempt("root", "sol", "low", RUNTIME_EFFORTS)
            self.assertTrue(ok, reason)
            sol_parents.append(thread_id)
        direct_luna = []
        for _ in range(root_luna_cap):
            ok, reason, thread_id = tree.attempt("root", "luna", "max", RUNTIME_EFFORTS)
            self.assertTrue(ok, reason)
            direct_luna.append(thread_id)
        nested_luna_count = self.policy["descendant_open_threads"] - root_sol_cap - root_luna_cap
        self.assertLessEqual((nested_luna_count + root_sol_cap - 1) // root_sol_cap,
                             per_sol_luna_cap)
        for i in range(nested_luna_count):
            ok, reason, _ = tree.attempt(sol_parents[i % root_sol_cap], "luna", "max", RUNTIME_EFFORTS)
            self.assertTrue(ok, reason)
        self.assertEqual(tree.total_open, self.policy["global_open_threads_including_root"])

        # This Sol child has an unused per-parent Luna slot, so rejection is global capacity.
        blocked_parent = sol_parents[0]
        ok, reason, _ = tree.attempt(blocked_parent, "luna", "max", RUNTIME_EFFORTS)
        self.assertFalse(ok)
        self.assertEqual(reason, "global-pool-full")

        # Wait marks completion, retaining the resident slot and its edge count.
        tree.mark_completed(direct_luna.pop())
        self.assertEqual(tree.total_open, self.policy["global_open_threads_including_root"])
        self.assertEqual(tree.edge_counts[("root", "luna")], root_luna_cap)

        # Retry triggers native V2 on-demand reclamation, then restores the full pool.
        ok, reason, _ = tree.attempt(blocked_parent, "luna", "max", RUNTIME_EFFORTS)
        self.assertTrue(ok, reason)
        self.assertEqual(tree.total_open, self.policy["global_open_threads_including_root"])

    def test_instruction_gates_cover_contract_escalation_pool_and_wikilayer(self):
        text = " ".join(self.skill.lower().split())
        before_spawn = text.index("before each spawn")
        contract_first = text.index("exact same contract as the first content")
        self.assertLess(before_spawn, contract_first)
        self.assertIn("record its complete contract", text)
        self.assertIn("fork_turns=\"none\"", text)

        self.assertIn("a child that needs stronger capability asks its parent", text)
        self.assertIn("a terra or luna child that needs stronger capability asks its parent", text)
        self.assertIn("inspect the existing agents and wait for a completion", text)
        self.assertIn("reuse a released slot", text)
        self.assertIn("or a spawn reports capacity", text)
        self.assertIn("do not busy-loop", text)
        self.assertIn("automatic queue", text)
        self.assertIn("native v2 may unload eligible idle completed contexts on demand", text)
        self.assertIn("two total attempts (initial plus one retry) by default", text)

        permissions = text.split("## permissions and wikilayer", 1)[1]
        self.assertIn("children never write wikilayer", permissions)
        self.assertIn("root agent alone writes", permissions)
        self.assertIn("completion phase", permissions)
        self.assertIn("root does not make interim wikilayer writes", permissions)

    def test_open_concurrency_is_not_lifetime_spawn_budget(self):
        self.assertEqual(self.policy["global_open_threads_including_root"], 50)
        self.assertEqual(self.policy["descendant_open_threads"], 49)
        self.assertIsNone(self.policy["lifetime_spawn_cap"])
        self.assertIn("simultaneous open immediate children", self.skill.lower())
        self.assertIn("no lifetime spawn cap", self.skill.lower())
        self.assertNotRegex(self.skill, r"at most 6 child spawns total")
        self.assertNotRegex(self.skill, r"depth 3 below root")

    def test_policy_does_not_bootstrap_from_an_external_service(self):
        # Routing must remain available with only the installed package + runtime.
        metadata = (SKILL.parent / "agents/openai.yaml").read_text()
        for document in (self.skill, metadata):
            self.assertNotRegex(document, r"get_page_markdown|get_node|rules_read_and_agreed")
            self.assertNotIn("load live WikiLayer policy", document)
        self.assertIn("self-contained", self.skill.lower())
        self.assertIn("no wikilayer connection is required", " ".join(self.skill.lower().split()))

    def test_local_skill_references_are_part_of_the_installed_package(self):
        for target in re.findall(r"\]\(([^)]+)\)", self.skill):
            if "://" in target or target.startswith("#"):
                continue
            path = (SKILL.parent / target.split("#", 1)[0]).resolve()
            self.assertTrue(path.is_relative_to(SKILL.parent.resolve()), target)
            self.assertTrue(path.is_file(), target)


if __name__ == "__main__":
    unittest.main()
