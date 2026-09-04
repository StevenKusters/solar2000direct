"""Validate the Home Assistant add-on manifest.

Worth its own test because the failure is silent: the Supervisor skips an add-on whose
config.yaml it cannot parse or validate, with nothing in the add-on store to say why. A
manifest was shipped once whose schema read `p1_phase_import: [str]?`, which is not even
valid YAML -- a flow sequence followed by a stray '?'. It was checked with a regex, which
happily matched, and the add-on simply never appeared.

Run with: python solar2000direct/tests/test_addon_manifest.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ADDON = ROOT / "solar2000direct"

SCALAR_TYPES = ("str", "bool", "int", "float", "password", "email", "url", "port", "list(")


def check(name: str, condition: object, detail: str = "") -> bool:
    """Report one check. Coerces to bool deliberately: callers accumulate results with
    `&=`, which is bitwise, so returning a truthy non-bool like a port number silently
    zeroes the accumulator and fails a run in which every check passed."""
    passed = bool(condition)
    print(f"  {'PASS' if passed else 'FAIL'}  {name}{'  -- ' + detail if detail and not passed else ''}")
    return passed


def main() -> int:
    ok = True

    # Parse with a real YAML parser, not a regex. That distinction is the whole point.
    try:
        config = yaml.safe_load((ADDON / "config.yaml").read_text())
        repository = yaml.safe_load((ROOT / "repository.yaml").read_text())
    except yaml.YAMLError as err:
        print(f"  FAIL  manifest is not valid YAML: {err}")
        return 1
    ok &= check("config.yaml and repository.yaml parse", True)

    for key in ("name", "version", "slug", "arch", "options", "schema"):
        ok &= check(f"config.yaml has {key!r}", key in config)

    options, schema = config["options"], config["schema"]
    ok &= check("every option has a schema entry", not (set(options) - set(schema)),
                f"missing: {sorted(set(options) - set(schema))}")

    # A schema key with no default must be optional: '?' for scalars, or a list type,
    # which the Supervisor treats as empty when absent.
    required_without_default = [
        key for key, value in schema.items()
        if key not in options and not isinstance(value, list)
        and not (isinstance(value, str) and value.endswith("?"))
    ]
    ok &= check("schema keys without defaults are optional", not required_without_default,
                f"not optional: {required_without_default}")

    list_keys = [key for key, value in schema.items() if isinstance(value, list)]
    ok &= check("list options default to an empty list",
                all(isinstance(options.get(key), list) for key in list_keys),
                f"lists: {list_keys}")

    ok &= check("no schema value uses the invalid [type]? form",
                not [k for k, v in schema.items() if isinstance(v, str) and v.startswith("[")])

    ok &= check("ingress is configured", config.get("ingress") and config.get("ingress_port"))
    # build.yaml is deprecated; the base image belongs in the Dockerfile. It must not be
    # taken from BUILD_FROM either -- the Supervisor passes its own Alpine base there,
    # which has no Python, and the build fails on a missing pip.
    dockerfile = (ADDON / "Dockerfile").read_text()
    # Comments explain why BUILD_FROM is avoided, so only real directives count.
    directives = [
        line.strip() for line in dockerfile.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    ok &= check("no build.yaml", not (ADDON / "build.yaml").exists())
    ok &= check("Dockerfile does not inherit BUILD_FROM",
                not any("BUILD_FROM" in line for line in directives))
    ok &= check("Dockerfile pins a Python base",
                any(line.startswith("FROM python:3.1") for line in directives))
    ok &= check("no deprecated architectures",
                not ({"armv7", "armhf", "i386"} & set(config["arch"])), str(config["arch"]))
    ok &= check("repository.yaml names the repo", bool(repository.get("name")))

    # Home Assistant shows CHANGELOG.md in the update dialog. Checking that the release
    # being shipped is actually described there is the only thing that stops the habit
    # lapsing the first time a change feels too small to write down.
    # Every option the UI shows should explain itself there. An option with a default but
    # no description is a number someone has to guess the meaning of.
    translations = ADDON / "translations" / "en.yaml"
    ok &= check("translations/en.yaml exists", translations.exists())
    if translations.exists():
        configuration = yaml.safe_load(translations.read_text()).get("configuration", {})
        described = set(configuration)
        # Against the schema, not against `options`. Nine keys existed in the schema with
        # no default listed in `options`, so this compared them against nothing and passed
        # while Home Assistant rendered them to the user as raw snake_case variable names.
        undescribed = set(schema) - described
        ok &= check("every visible option is described", not undescribed,
                    f"undescribed: {sorted(undescribed)}")
        unknown = described - set(schema)
        ok &= check("no description for an option that does not exist", not unknown,
                    f"unknown: {sorted(unknown)}")
        # Each entry must be a mapping carrying at least a name. Written as a bare string
        # it is still valid YAML and still "described", but Home Assistant cannot read it
        # and falls back to raw keys -- for the whole screen, not just that one option.
        # Shipped exactly that way once, and the options page went back to naming every
        # setting by its variable name.
        malformed = sorted(
            key for key, value in configuration.items()
            if not isinstance(value, dict) or not value.get("name")
        )
        ok &= check("every description is a mapping with a name", not malformed,
                    f"malformed: {malformed}")

        # A description that says what a field means but not what to type in it leaves the
        # reader guessing at the format. "In string order, for example 8 and 12" does not
        # say whether the box wants "8 and 12", "8,12", or two separate entries -- and
        # the answer is the third, which nobody would pick from that sentence.
        def described_as(key: str) -> str:
            return str(configuration.get(key, {}).get("description", "")).lower()

        # Home Assistant renders a list option as a repeating field, one value per entry,
        # which is the least guessable widget on the page.
        lists = [key for key, value in schema.items() if isinstance(value, list)]
        vague = [
            key for key in lists
            if not any(phrase in described_as(key)
                       for phrase in ("one at a time", "repeating field"))
        ]
        ok &= check("every repeating field says entries are added one at a time", not vague,
                    f"vague: {sorted(vague)}")

        # Fields taking a Home Assistant entity must show a whole one, domain included.
        entity_options = [key for key in schema if key.startswith("p1_")]
        without_example = [key for key in entity_options if "sensor." not in described_as(key)]
        ok &= check("every entity field shows a complete entity ID", not without_example,
                    f"no example: {sorted(without_example)}")

        # Prices are the classic units trap: cents typed where units are wanted is a
        # hundredfold error that looks plausible on the page.
        prices = [key for key in schema if "price" in key or "tariff_per" in key]
        unpriced = [key for key in prices if not re.search(r"\d+\.\d+", described_as(key))]
        ok &= check("every price field shows a worked figure", not unpriced,
                    f"no example: {sorted(unpriced)}")

        # The dashboard tells a reader which setting to go and change, quoting it by the
        # label the options page shows. Those labels live in this file, so renaming one
        # here silently sends the reader looking for a field that no longer exists.
        page = (ADDON / "src" / "solar2000direct" / "web" / "index.html").read_text()
        quoted = set(re.findall(r'field\("([^"]+)"\)', page))
        quoted |= set(re.findall(r'setting\("([^"]+)"', page))
        known = {str(value.get("name")) for value in configuration.values() if isinstance(value, dict)}
        stale = sorted(quoted - known)
        ok &= check("every setting the dashboard names still exists", not stale,
                    f"no such option label: {stale}")
        ok &= check("the dashboard names at least one setting", quoted,
                    "field()/setting() found nowhere -- has the helper been renamed?")

    # A worked example is only useful while it still matches the options it claims to
    # document. Kept honest here rather than by remembering to update it.
    example_path = ADDON / "example-config.yaml"
    ok &= check("example-config.yaml exists", example_path.exists())
    if example_path.exists():
        example = yaml.safe_load(example_path.read_text())
        ok &= check("the example is a mapping", isinstance(example, dict))
        if isinstance(example, dict):
            invented = sorted(set(example) - set(schema))
            ok &= check("the example sets no option that does not exist", not invented,
                        f"invented: {invented}")
            skipped = sorted(set(options) - set(example))
            ok &= check("the example covers every option the add-on offers", not skipped,
                        f"undocumented: {skipped}")
            wrong = []
            for key, value in example.items():
                spec = schema.get(key)
                if isinstance(spec, list):
                    if not isinstance(value, list):
                        wrong.append(key)
                    continue
                base = str(spec).rstrip("?").split("(")[0]
                expected = {"str": str, "int": int, "float": (int, float),
                            "bool": bool, "password": str}.get(base)
                if base == "list" or expected is None or value == "":
                    continue
                if not isinstance(value, expected):
                    wrong.append(f"{key} wants {base}")
            ok &= check("every example value matches its declared type", not wrong,
                        f"wrong: {wrong}")
            # It ships to strangers, so it must not carry anyone's real installation.
            # Written as patterns rather than as a list of one maintainer's own details,
            # which would have put those details in a public repository to guard against
            # publishing them.
            #
            # Over every file that documents a worked example, not just this one. Checking
            # example-config.yaml alone is why .env.example reached a public commit still
            # carrying the maintainer's own contract prices, roof layout and Home Assistant
            # entity IDs: it is the same worked example in another syntax, and nothing
            # looked at it.
            worked_examples = [example_path, ADDON / ".env.example", ADDON / "tests" / "mock_dashboard.py"]
            for path in worked_examples:
                if not path.exists():
                    continue
                text = path.read_text()
                leaked = re.findall(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b", text)
                leaked += re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
                ok &= check(f"{path.name} carries no real address or mailbox", not leaked,
                            f"found: {sorted(set(leaked))}")

            # The two example files describe ONE house, so where they overlap they must
            # agree. Divergence is the signal that one of them is somebody's real config:
            # that is exactly how .env.example read, with prices and an array that matched
            # no documented example anywhere in the repository.
            env_path = ADDON / ".env.example"
            if env_path.exists():
                env = dict(
                    line.split("=", 1) for line in env_path.read_text().splitlines()
                    if "=" in line and not line.lstrip().startswith("#"))
                counts = ",".join(str(c) for c in example.get("string_panel_counts", []))
                agree = {
                    "S2D_STRING_PANEL_COUNTS": counts,
                    "S2D_PANEL_WATTS": str(example.get("panel_watts", "")),
                    "S2D_ENERGY_PRICE_PER_KWH": f"{example.get('energy_price_per_kwh', 0):.4f}",
                    "S2D_FEED_IN_PRICE_PER_KWH": f"{example.get('feed_in_price_per_kwh', 0):.4f}",
                }
                differing = [
                    f"{key}={env.get(key)!r} vs example-config {value!r}"
                    for key, value in agree.items() if env.get(key, "").strip() != value
                ]
                ok &= check("the two example files describe the same house", not differing,
                            f"differing: {differing}")

            # Entity IDs are the giveaway: a name Home Assistant generated in one person's
            # instance cannot be a placeholder. Both example files use the p1_meter family.
            for path in (example_path, env_path):
                if not path.exists():
                    continue
                foreign = [
                    name for name in re.findall(r"sensor\.[\w.]+", path.read_text())
                    if not name.startswith("sensor.p1_meter")
                ]
                ok &= check(f"{path.name} uses only placeholder entity IDs", not foreign,
                            f"found: {sorted(set(foreign))}")

    # Presentation files the Supervisor looks for by name. Without DOCS.md the add-on page
    # has no Documentation tab at all; without the images it gets a generic placeholder.
    for filename, purpose in (
        ("DOCS.md", "the Documentation tab"),
        ("icon.png", "the add-on icon"),
        ("logo.png", "the store logo"),
    ):
        ok &= check(f"{filename} exists for {purpose}", (ADDON / filename).exists())

    # Three files carry the version and they had drifted eleven releases apart: the wheel
    # built into the image reported one number while the add-on reported another.
    pyproject = (ADDON / "pyproject.toml").read_text()
    init = (ADDON / "src" / "solar2000direct" / "__init__.py").read_text()
    version = str(config["version"])
    ok &= check("pyproject.toml carries the add-on version",
                f'version = "{version}"' in pyproject,
                f"config.yaml says {version}")
    ok &= check("__init__.py carries the add-on version",
                f'__version__ = "{version}"' in init,
                f"config.yaml says {version}")

    changelog = ADDON / "CHANGELOG.md"
    ok &= check("CHANGELOG.md exists", changelog.exists())
    if changelog.exists():
        text = changelog.read_text()
        version = str(config["version"])
        ok &= check(f"changelog documents {version}", f"## {version}" in text,
                    "add a '## <version>' section for this release")
        # Home Assistant filters the changelog to the entries between the installed and
        # the latest version, with `^#* {version}\n` -- the version must be the whole rest
        # of the line. A heading like "## 0.7.4 - faster charts" fails to match and the
        # reader is shown the entire changelog instead of what they are about to install.
        titled = [
            line for line in text.splitlines()
            if re.match(r"^#+ \d+\.\d+", line) and not re.match(r"^#+ \S+$", line)
        ]
        ok &= check("every changelog heading is a bare version", not titled,
                    f"Home Assistant cannot match: {titled}")

        headings = re.findall(r"^## (\S+)", text, re.M)
        ok &= check("newest changelog entry is this version",
                    bool(headings) and headings[0] == version,
                    f"top entry is {headings[0] if headings else 'none'}, config says {version}")

    # Every option the user can set must reach the application as an environment variable.
    sys.path.insert(0, str(ADDON / "src"))
    from solar2000direct.addon import OPTION_ENV

    supervisor_supplied = {
        "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password",
        "home_assistant_url", "home_assistant_token",
    }
    unhandled = set(schema) - set(OPTION_ENV) - supervisor_supplied
    ok &= check("every schema option is translated to an env var", not unhandled,
                f"unhandled: {sorted(unhandled)}")
    orphaned = set(OPTION_ENV) - set(schema)
    ok &= check("no env translation refers to a missing option", not orphaned,
                f"orphaned: {sorted(orphaned)}")

    print("\n" + ("all checks passed" if ok else "MANIFEST IS BROKEN"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
