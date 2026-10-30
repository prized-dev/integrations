from minisweagent.run.benchmarks import swebench as upstream

from minisweagent_prized import swebench

INSTANCE = {"instance_id": "django__django-11099", "problem_statement": "x"}


def test_prized_gets_the_instance_image(monkeypatch):
    made = []
    monkeypatch.setattr(swebench, "get_environment", lambda config: made.append(config) or "env")
    config = {"environment": {"environment_class": "prized", "cwd": "/testbed", "timeout": 60}}
    assert swebench.get_sb_environment(config, INSTANCE) == "env"
    assert made == [
        {
            "environment_class": "minisweagent_prized.PrizedEnvironment",
            "cwd": "/testbed",
            "timeout": 60,
            "image": "docker.io/swebench/sweb.eval.x86_64.django_1776_django-11099:latest",
        }
    ]
    assert config["environment"]["environment_class"] == "prized"  # the caller's config is left as it was


def test_startup_command_runs_in_the_sandbox(monkeypatch):
    class Env:
        def execute(self, action):
            self.ran = action
            return {"returncode": 0, "output": ""}

    env = Env()
    monkeypatch.setattr(swebench, "get_environment", lambda config: env)
    config = {
        "environment": {"environment_class": "minisweagent_prized.PrizedEnvironment"},
        "run": {"env_startup_command": "echo {{instance_id}}"},
    }
    swebench.get_sb_environment(config, INSTANCE)
    assert env.ran == {"command": "echo django__django-11099"}


def test_other_environments_go_to_mini_swe_agent(monkeypatch):
    monkeypatch.setattr(swebench, "_original_get_sb_environment", lambda config, instance: "docker-env")
    assert swebench.get_sb_environment({"environment": {"environment_class": "docker"}}, INSTANCE) == "docker-env"


def test_main_installs_and_defaults_to_prized(monkeypatch):
    seen = []
    monkeypatch.setattr(upstream, "app", lambda argv: seen.append(argv))
    monkeypatch.setattr(upstream, "get_sb_environment", upstream.get_sb_environment)
    swebench.main(["--subset", "verified", "-w", "4"])
    assert seen == [["--subset", "verified", "-w", "4", "--environment-class", "prized"]]
    assert upstream.get_sb_environment is swebench.get_sb_environment
    swebench.main(["--environment-class=docker"])
    assert seen[-1] == ["--environment-class=docker"]
