import pytest

from agent_launcher.redact import REDACTED, redact_args, redact_env, redact_text, redact_value

HOME = "/Users/alice"


@pytest.mark.parametrize(
    "name",
    ["GITHUB_TOKEN", "OPENAI_API_KEY", "anthropic_api_key", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY", "MY_AUTH", "SESSION_COOKIE"],
)
def test_secret_looking_env_values_are_redacted(name):
    assert redact_env({name: "hunter2"}) == {name: REDACTED}


def test_ordinary_env_values_are_kept():
    env = {"CLAUDE_CONFIG_DIR": "/opt/claude", "EDITOR": "vim", "GIT_AUTHOR_NAME": "Alice"}
    assert redact_env(env) == env


def test_env_values_with_token_shapes_are_redacted_even_under_innocent_names():
    out = redact_env({"NOTE": "ghp_" + "a" * 36})
    assert "ghp_" not in out["NOTE"]


def test_token_style_args():
    assert redact_args(["--token", "abc123", "--verbose"]) == ["--token", REDACTED, "--verbose"]
    assert redact_args(["--api-key=abc123"]) == [f"--api-key={REDACTED}"]
    assert redact_args(["--password", "pw", "run"]) == ["--password", REDACTED, "run"]
    assert redact_args(["GITHUB_TOKEN=abc"]) == [f"GITHUB_TOKEN={REDACTED}"]


def test_plain_args_are_kept():
    args = ["--model", "opus", "--resume", "abc"]
    assert redact_args(args) == args


def test_prompt_arguments_are_omitted():
    out = redact_args(["--prompt", "fix the bug in auth.py", "-p", "do x"])
    assert out == ["--prompt", "[PROMPT OMITTED: 22 chars]", "-p", "[PROMPT OMITTED: 4 chars]"]
    assert redact_args(["line1\nline2"]) == ["[PROMPT OMITTED: 11 chars]"]
    assert redact_args(["x" * 500]) == ["[PROMPT OMITTED: 500 chars]"]


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://user:pass@github.com/o/r.git", f"https://{REDACTED}@github.com/o/r.git"),
        ("https://ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/o/r", f"https://{REDACTED}@github.com/o/r"),
        ("clone ssh://git:secret@host:22/x failed", f"clone ssh://{REDACTED}@host:22/x failed"),
    ],
)
def test_urls_with_credentials(url, expected):
    assert redact_text(url) == expected


def test_urls_without_credentials_are_kept():
    assert redact_text("https://github.com/o/r/issues/4") == "https://github.com/o/r/issues/4"


@pytest.mark.parametrize(
    "text",
    [
        "gho_" + "A" * 36,
        "github_pat_" + "B" * 30,
        "sk-" + "c" * 30,
        "xoxb-1234567890-abcdef",
        "AKIAABCDEFGHIJKLMNOP",
        "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl",
        "Bearer abcdefgh12345678",
    ],
)
def test_token_shapes(text):
    out = redact_text(f"value {text} end")
    assert text.split()[-1] not in out
    assert "end" in out


def test_key_value_in_free_text_and_json():
    assert redact_text("password=hunter2 next") == f"password={REDACTED} next"
    assert redact_text('{"api_key": "abc"}') == f'{{"api_key": {REDACTED}}}'
    assert "abc" not in redact_text("export ANTHROPIC_API_KEY='abc'")


def test_home_and_sensitive_paths():
    assert redact_text("/Users/alice/work/repo", HOME) == "~/work/repo"
    assert redact_text("/home/bob/repo") == "~/repo"
    out = redact_text("read /Users/alice/.ssh/id_ed25519 and ~/.aws/credentials", HOME)
    assert "id_ed25519" not in out and "credentials" not in out
    assert "~/[REDACTED-PATH]" in out
    assert "env" not in redact_text("~/proj/.env.local").replace("[REDACTED-PATH]", "")


def test_redact_value_walks_structures():
    config = {
        "profiles": {
            "work": {
                "agents": {
                    "claude": {
                        "env": {"GH_TOKEN": "t0ken", "CLAUDE_CONFIG_DIR": "/Users/alice/.claude-work"},
                        "args": ["--token", "t0ken", "--model", "opus"],
                    }
                }
            }
        },
        "api_key": "k",
        "debug": True,
    }
    out = redact_value(config, HOME)
    inst = out["profiles"]["work"]["agents"]["claude"]
    assert inst["env"] == {"GH_TOKEN": REDACTED, "CLAUDE_CONFIG_DIR": "~/.claude-work"}
    assert inst["args"] == ["--token", REDACTED, "--model", "opus"]
    assert out["api_key"] == REDACTED and out["debug"] is True
    assert "t0ken" not in str(out)


def test_flags_in_free_text_and_prose_survive():
    assert redact_text("ran --token abc123 ok") == f"ran --token {REDACTED} ok"
    assert redact_text("--api-key=abc123") == f"--api-key={REDACTED}"
    assert redact_text("Run `gh auth login`.") == "Run `gh auth login`."


def test_home_pattern_only_matches_at_path_start():
    assert redact_text("/tmp/x/home/config.json") == "/tmp/x/home/config.json"
    assert redact_text("at /home/bob/x and /Users/eve/y") == "at ~/x and ~/y"


@pytest.mark.parametrize("name", ["OPENAI_KEY", "HF_KEY", "DB_PASS", "GITHUB_PAT", "key", "api.key", "Authorization", "PROXY_AUTHORIZATION"])
def test_key_pass_pat_authorization_names_are_secret(name):
    assert redact_env({name: "v"}) == {name: REDACTED}


@pytest.mark.parametrize("name", ["KEYBOARD", "PATH", "PASSENGER", "PATTERN", "AUTHOR", "GIT_AUTHOR_NAME", "MONKEY", "COMPASS"])
def test_lookalike_names_are_not_secret(name):
    assert redact_env({name: "v"}) == {name: "v"}


def test_key_pass_pat_args():
    assert redact_args(["--key", "abc", "--pass", "pw", "--pat", "x", "OPENAI_KEY=abc", "--keyboard", "us"]) == [
        "--key", REDACTED, "--pass", REDACTED, "--pat", REDACTED, f"OPENAI_KEY={REDACTED}", "--keyboard", "us",
    ]
    assert redact_args(["--key=abc"]) == [f"--key={REDACTED}"]


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: token abcdef0123456789",
        "authorization: Basic dXNlcjpwYXNz",
        "Proxy-Authorization: Negotiate abc def ghi",
        "-H 'Authorization: Bearer abcdefgh12345678'",
        '"Authorization": "token abcdef0123456789"',
    ],
)
def test_authorization_headers_are_redacted_to_end_of_line(line):
    out = redact_text(f"> GET /x\n{line}\n> Accept: json")
    assert "abcdef" not in out and "dXNlcjpwYXNz" not in out and "def ghi" not in out
    assert "> GET /x" in out and "> Accept: json" in out


def test_allowlist_env_keeps_only_safe_names():
    from agent_launcher.redact import redact_env_allowlist

    out = redact_env_allowlist({"PATH": "/usr/bin", "CODEX_HOME": "/Users/alice/.codex-w", "FOO": "bar", "OPENAI_KEY": "k"}, HOME)
    assert out == {"PATH": "/usr/bin", "CODEX_HOME": "~/.codex-w", "FOO": REDACTED, "OPENAI_KEY": REDACTED}


def test_copilot_home_is_shown_like_codex_home():
    from agent_launcher.redact import redact_env_allowlist

    out = redact_env_allowlist({"COPILOT_HOME": "/Users/alice/.copilot-w", "COPILOT_GITHUB_TOKEN": "t"}, HOME)
    assert out == {"COPILOT_HOME": "~/.copilot-w", "COPILOT_GITHUB_TOKEN": REDACTED}
