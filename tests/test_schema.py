import json
import unittest

from helpers import ROOT
from engine import config, schema, workflow


class SchemaConventionTest(unittest.TestCase):
    """Every setting is <tab>.<section>.<setting>, in the settings files, the defaults and the settings form."""

    @classmethod
    def setUpClass(cls):
        wf = workflow.load("sdlc", config.command_names("sdlc", {}))
        agents = config.agents_of(wf["stages"])
        cls.stages = list(wf["stages"])
        cls.defaults = config.defaults("sdlc", cls.stages, agents)
        cls.schema = schema.build("sdlc", cls.stages, agents)

    def test_every_field_path_follows_tab_section_setting_and_has_a_default(self):
        for tab in self.schema["tabs"]:
            for sec in tab["sections"]:
                for f in sec.get("fields", []):
                    parts = f["path"].split(".")
                    self.assertEqual(parts[:2], [tab["id"], sec["id"]], f["path"])
                    self.assertEqual(len(parts), 3, f["path"])
                    cur = self.defaults
                    for k in parts:
                        self.assertIn(k, cur, f"{f['path']} is not a default")
                        cur = cur[k]

    def test_every_default_setting_is_in_the_form(self):
        form = {f["path"] for t in self.schema["tabs"] for s in t["sections"] for f in s.get("fields", [])}
        collections = {f"{t['id']}.{s['id']}" for t in self.schema["tabs"] for s in t["sections"] if s.get("collection")}
        free = {f"{t['id']}.{s['id']}" for t in self.schema["tabs"] for s in t["sections"] if s.get("freeform")}
        for tab, secs in self.defaults.items():
            for sec, settings in secs.items():
                if f"{tab}.{sec}" in free:
                    continue
                if f"{tab}.{sec}" in collections:
                    self.assertEqual(list(settings), ["list"], f"{tab}.{sec}")
                    continue
                for name in settings:
                    self.assertIn(f"{tab}.{sec}.{name}", form, "not in the settings form")

    def test_the_form_has_a_section_per_stage_and_per_agent(self):
        tabs = {t["id"]: t for t in self.schema["tabs"]}
        self.assertEqual([s["id"] for s in tabs["stage"]["sections"]], self.stages)
        self.assertIn("spec-writer", [s["id"] for s in tabs["agent"]["sections"]])
        release = next(s for s in tabs["stage"]["sections"] if s["id"] == "release")
        self.assertIn("stage.release.strategy", {f["path"] for f in release["fields"]})     # the workflow's own setting
        self.assertIn("stage.release.rounds", {f["path"] for f in release["fields"]})       # and the engine's

    def test_inherited_settings_point_at_real_settings(self):
        for t in self.schema["tabs"]:
            for s in t["sections"]:
                for f in s.get("fields", []):
                    if f.get("inherits"):
                        cur = self.defaults
                        for k in f["inherits"].split("."):
                            cur = cur[k]

    def test_the_engine_schema_holds_no_pack_defaults_and_the_workflow_schema_all_of_its_own(self):
        eng = json.loads((ROOT / "templates" / "epi.schema.json").read_text())
        for t in eng["tabs"]:
            for s in t.get("sections", []):
                for f in s.get("fields", []):
                    self.assertNotIn("default", f, f["path"])       # engine defaults live in config.DEFAULTS
        wf = schema.workflow_schema("sdlc")
        for t in wf["tabs"]:
            for s in t["sections"]:
                for f in s["fields"]:
                    self.assertIn("default", f, f["path"])

    def all_fields(self):
        return [(t["id"], f) for t in self.schema["tabs"] for s in t["sections"] for f in s.get("fields", [])]

    def test_every_field_has_a_plain_label_and_help(self):
        jargon = ("job", "pack", "reopen", "stalemate", "referee", "gate", "override", "rework", "substep")
        for tab, f in self.all_fields():
            self.assertTrue(f.get("label"), f["path"])
            self.assertNotRegex(f["label"], r"[_.]", f["path"])                         # not a raw key
            for word in jargon:
                self.assertNotIn(word, (f["label"] + " " + f["help"]).lower(), f["path"])

    def test_every_global_field_is_basic_or_advanced(self):
        for tab, f in self.all_fields():
            if tab == "global":
                self.assertIn(f.get("level"), ("basic", "advanced"), f["path"])
            else:
                self.assertNotIn("level", f, f["path"])

    def test_basic_settings_are_the_few_a_person_needs(self):
        basic = {f["path"] for tab, f in self.all_fields() if f.get("level") == "basic"}
        self.assertEqual(basic, {"global.command.build", "global.command.lint", "global.command.test", "global.command.setup",
                                 "global.budget.per_task", "global.budget.per_month",
                                 "global.pipeline.critical_thinking", "global.learning.mode"})

    def test_token_settings_say_so_for_display(self):
        tokens = {f["path"] for tab, f in self.all_fields() if f.get("unit") == "tokens"}
        self.assertEqual(tokens, {"global.budget.per_task", "global.budget.per_month", "global.budget.orchestrate",
                              "global.budget.context_window"}
                         | {f"stage.{s}.budget_tokens" for s in self.stages})

    def test_where_a_field_applies_is_declared_with_known_keys(self):
        known = {"kind": ("agent", "skill"), "writes": ("output", "worktree", "paths")}
        applies = {f["path"]: f["applies"] for tab, f in self.all_fields() if f.get("applies")}
        self.assertTrue(any(p.endswith(".effort") for p in applies))
        for path, a in applies.items():
            for k, v in a.items():
                self.assertIn(k, known, path)
                for x in (v if isinstance(v, list) else [v]):
                    self.assertIn(x, known[k], path)


if __name__ == "__main__":
    unittest.main()
