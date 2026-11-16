import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import instrument  # noqa: E402

GATE = "[target.'cfg(all(not(target_family=\"wasm\"), not(target_arch=\"riscv32\")))'.dependencies]"

SOURCE = '''\
//! fn in_a_comment() {}
use std::fmt;

/// Documented.
#[inline]
pub(crate) fn free<'a>(text: &'a str) -> impl fmt::Display + 'a { text }

pub const fn constant() -> u8 { b'{' }

pub struct Thing<T>(T);

impl<T: Clone> Thing<T> {
    pub async fn method(&self) -> char { '}' }
    pub unsafe extern "C" fn foreign(&self) {}
    fn takes_pointer(&self, f: fn(u8) -> u8) -> u8 {
        fn nested(x: u8) -> u8 { x }
        f(nested(1))
    }
}

impl<'a, T> From<&'a T> for crate::Thing<T> where T: Clone {
    fn from(value: &'a T) -> Self { Thing(value.clone()) }
}

pub trait Shape {
    fn declared(&self) -> impl Iterator<Item = u8>;
    fn provided(&self) -> usize { self.declared().count() }
}

macro_rules! make {
    ($name:ident) => { fn $name() {} fn fixed() {} };
}

fn text() -> &'static str { r#"fn in_a_string() { "# }

extern "C" {
    fn imported(x: i32) -> i32;
}

#[cfg(test)]
mod tests {
    fn helper() {}
    #[test]
    fn checks() { helper() }
}

#[cfg(test)]
mod elsewhere;

#[test]
fn stray_test() {}
'''


def make_tree(root: Path, manifest_tail: str = "") -> Path:
    (root / "Cargo.toml").write_text('[workspace.dependencies]\namaru-freeze = { path = "crates/amaru-freeze" }\n')
    crate = root / "crates" / "demo"
    (crate / "src").mkdir(parents=True)
    (crate / "tests").mkdir()
    (crate / "Cargo.toml").write_text('[package]\nname = "demo"\n\n[dependencies]\nserde = "1"\n' + manifest_tail)
    (crate / "src" / "lib.rs").write_text(SOURCE)
    (crate / "src" / "elsewhere.rs").write_text("fn only_in_tests() {}\n")
    (crate / "tests" / "it.rs").write_text("fn integration() {}\n")
    return crate


def test_every_function_with_a_body_gets_the_attribute_once(tmp_path):
    crate = make_tree(tmp_path)
    assert instrument.add_tracing(tmp_path, ["demo"]) == {"demo": 8}
    source = (crate / "src" / "lib.rs").read_text()

    assert "/// Documented.\n#[inline]\n#[amaru_freeze::trace_fn]\npub(crate) fn free<'a>" in source
    assert "\npub const fn constant()" in source
    assert '    #[amaru_freeze::trace_fn(in = "Thing")]\n    pub async fn method' in source
    assert '    #[amaru_freeze::trace_fn(in = "Thing")]\n    pub unsafe extern "C" fn foreign' in source
    assert '    #[amaru_freeze::trace_fn(in = "Thing")]\n    fn takes_pointer' in source
    assert '        #[amaru_freeze::trace_fn(in = "Thing::takes_pointer")]\n        fn nested' in source
    assert '    #[amaru_freeze::trace_fn(in = "Thing")]\n    fn from(' in source
    assert "\n    fn declared(&self)" in source
    assert '    #[amaru_freeze::trace_fn(in = "Shape")]\n    fn provided' in source
    assert "#[amaru_freeze::trace_fn]\nfn text()" in source
    for untouched in ("{ fn $name() {} fn fixed() {} }", "\n    fn imported(", "\n    fn helper()", "\n    fn checks()",
                      "#[test]\nfn stray_test()"):
        assert untouched in source
    assert source.replace("#[amaru_freeze::trace_fn]\n", "").count("trace_fn") == 6

    assert (crate / "src" / "elsewhere.rs").read_text() == "fn only_in_tests() {}\n"
    assert (crate / "tests" / "it.rs").read_text() == "fn integration() {}\n"
    assert "[dependencies]\namaru-freeze.workspace = true\nserde" in (crate / "Cargo.toml").read_text()

    assert instrument.add_tracing(tmp_path, ["demo"]) == {"demo": 0}
    assert (crate / "src" / "lib.rs").read_text() == source


def test_a_portable_crate_keeps_the_dependency_and_the_attribute_behind_its_condition(tmp_path):
    crate = make_tree(tmp_path, f"\n{GATE}\namaru-deps = \"1\"\n")
    assert instrument.add_tracing(tmp_path, ["demo"]) == {"demo": 8}
    assert f"{GATE}\namaru-freeze.workspace = true\namaru-deps" in (crate / "Cargo.toml").read_text()
    condition = 'all(not(target_family = "wasm"), not(target_arch = "riscv32"))'
    source = (crate / "src" / "lib.rs").read_text()
    assert f'#[cfg_attr({condition}, amaru_freeze::trace_fn(in = "Shape"))]\n    fn provided' in source
    assert f"#[cfg_attr({condition}, amaru_freeze::trace_fn)]\nfn text()" in source
    assert instrument.add_tracing(tmp_path, ["demo"]) == {"demo": 0}


def test_the_command_line_counts_without_writing_on_a_dry_run(tmp_path, capsys):
    crate = make_tree(tmp_path)
    assert instrument.main(["trace", "--source", str(tmp_path), "--crate", "demo", "--dry-run"]) == 0
    assert '"demo": 8' in capsys.readouterr().out
    assert (crate / "src" / "lib.rs").read_text() == SOURCE
    assert "amaru-freeze" not in (crate / "Cargo.toml").read_text()
    assert instrument.main(["trace", "--source", str(tmp_path), "--crate", "missing"]) == 2


# --------------------------------------------------------------------------------------------
# Functions inside macros

MACROS = '''\
macro_rules! accessors {
    ($(#[$meta:meta])* $vis:vis $name:ident, $ty:ty, $body:block) => {
        $vis fn $name(&self) -> $ty { self.0 }
        $(#[$meta])* fn with_unknown_attributes() {}
        pub const fn in_a_constant() -> u8 { 1 }
        fn declared_only();
        fn unknown_body() $body
        impl Fixed {
            fn of_fixed(&self) {}
        }
        impl Shape for $ty {
            fn of_unknown(&self) {}
        }
        #[amaru_freeze::trace_fn]
        fn already() {}
        #[test]
        fn a_test() {}
        tokio::select! { _ = async { fn in_external_inside_body() {} } => {} }
    };
    () => { fn second_rule() { fn nested() {} } };
}

#[macro_export]
macro_rules! shared {
    () => { pub fn from_shared() {} };
}

#[macro_export]
macro_rules! leaked {
    () => { pub fn from_leaked() {} };
}

#[cfg(test)]
macro_rules! only_in_tests {
    () => { fn test_helper() {} };
}

pub struct Fixed(u8);

impl Fixed {
    items! {
        /// Kept.
        pub fn through_arguments(&self) -> u8 { self.0 }
        #[test]
        fn test_in_arguments() {}
        const fn constant_in_arguments() -> u8 { 1 }
    }
}

crate::items! {
    pub fn free_through_arguments() {}
    impl Fixed {
        fn method_through_arguments(&self) {}
    }
    demo::items! { fn nested_invocation() {} }
    serde_json::json!({ "f": { fn inside_external_inside_arguments() {} } });
}

tokio::select! {
    _ = async { fn in_external() {} } => {}
}

other::items! { fn in_external_of_the_same_name() {} }

bitflags! { fn in_unknown() {} }

pub fn plain() -> u8 {
    let value = items!(fn in_an_expression() {});
    items! { fn in_a_statement() {} }
    1
}
'''

ITEMS = '''\
#[macro_export]
macro_rules! items {
    ($($item:item)*) => { $($item)* };
}
'''


def make_macro_tree(root: Path) -> Path:
    """A workspace of `demo` (traced), `friend` (traced) and `stranger` (not traced)."""
    crate = make_tree(root)
    (crate / "src" / "macros.rs").write_text(MACROS)
    (crate / "src" / "items.rs").write_text(ITEMS)
    for name, source in (("friend", "demo::shared!();\nfn friendly() {}\n"), ("stranger", "demo::leaked!();\n")):
        other = root / "crates" / name
        (other / "src").mkdir(parents=True)
        manifest = f'[package]\nname = "{name}"\n\n[dependencies]\ndemo = {{ path = "../demo" }}\n'
        (other / "Cargo.toml").write_text(manifest)
        (other / "src" / "lib.rs").write_text(source)
    return crate


MACRO_FILES = {"crates/demo/src/macros.rs", "crates/demo/src/lib.rs", "crates/friend/src/lib.rs"}


def test_functions_in_macro_bodies_and_in_arguments_of_the_trees_macros_are_marked(tmp_path):
    crate = make_macro_tree(tmp_path)
    report = {}
    counts = instrument.add_tracing(tmp_path, ["demo", "friend"], macro_files=MACRO_FILES, macros=report)
    source = (crate / "src" / "macros.rs").read_text()
    mark = "#[amaru_freeze::trace_fn]"

    # In the body of a macro_rules!: the attribute goes in front of `$vis`, and names the owner
    # only where the body itself says which type it is.
    assert f"        {mark}\n        $vis fn $name(&self) -> $ty" in source
    assert '            #[amaru_freeze::trace_fn(in = "Fixed")]\n            fn of_fixed' in source
    assert f"            {mark}\n            fn of_unknown" in source
    nested = '#[amaru_freeze::trace_fn(in = "second_rule")] fn nested() {}'
    assert f"() => {{ {mark} fn second_rule() {{ {nested} }} }};" in source
    assert f"() => {{ {mark} pub fn from_shared() {{}} }};" in source
    for untouched in (
        "        $(#[$meta])* fn with_unknown_attributes",
        "        pub const fn in_a_constant",
        "        fn declared_only();",
        "        fn unknown_body() $body",
        f"        {mark}\n        fn already() {{}}",
        "#[test]\n        fn a_test",
        "{ fn in_external_inside_body() {} }",
        "() => { pub fn from_leaked() {} };",  # exported, and used by a crate without the agent
        "() => { fn test_helper() {} };",
    ):
        assert untouched in source

    # In the arguments of a macro the tree defines, where an item stands.
    assert '        #[amaru_freeze::trace_fn(in = "Fixed")]\n        pub fn through_arguments' in source
    assert f"    {mark}\n    pub fn free_through_arguments" in source
    assert '        #[amaru_freeze::trace_fn(in = "Fixed")]\n        fn method_through_arguments' in source
    assert f"demo::items! {{ {mark} fn nested_invocation() {{}} }}" in source
    for untouched in (
        "#[test]\n        fn test_in_arguments",
        "        const fn constant_in_arguments",
        "{ fn inside_external_inside_arguments() {} }",
        "{ fn in_external() {} }",
        "other::items! { fn in_external_of_the_same_name() {} }",
        "bitflags! { fn in_unknown() {} }",
        "items!(fn in_an_expression() {})",
        "items! { fn in_a_statement() {} }",
    ):
        assert untouched in source
    assert f"{mark}\npub fn plain()" in source

    lib = (crate / "src" / "lib.rs").read_text()
    assert f"{{ {mark} fn $name() {{}} {mark} fn fixed() {{}} }}" in lib

    assert report == {
        "files": {"crates/demo/src/macros.rs": 10, "crates/demo/src/lib.rs": 2},
        "reverted": {},
        "functions": 12,
    }
    assert counts == {"demo": 8 + 1 + 12, "friend": 1}
    assert sum(counts.values()) == 22 and ", ".join(counts) == "demo, friend"

    # Twice changes nothing.
    again = {}
    assert instrument.add_tracing(tmp_path, ["demo", "friend"], macro_files=MACRO_FILES, macros=again) == {
        "demo": 0,
        "friend": 0,
    }
    assert again == {"files": {}, "reverted": {}, "functions": 0}
    assert (crate / "src" / "macros.rs").read_text() == source
    assert (crate / "src" / "lib.rs").read_text() == lib


def test_an_exported_macro_is_marked_only_when_every_crate_using_it_is_traced(tmp_path):
    crate = make_macro_tree(tmp_path)
    instrument.add_tracing(tmp_path, ["demo"], macro_files=MACRO_FILES)
    source = (crate / "src" / "macros.rs").read_text()
    assert "() => { pub fn from_shared() {} };" in source  # `friend` uses it and is not traced
    assert "#[amaru_freeze::trace_fn] fn second_rule()" in source


def test_a_portable_crate_gets_the_condition_inside_macros_too(tmp_path):
    crate = make_macro_tree(tmp_path)
    (crate / "Cargo.toml").write_text((crate / "Cargo.toml").read_text() + f'\n{GATE}\namaru-deps = "1"\n')
    instrument.add_tracing(tmp_path, ["demo", "friend"], macro_files=MACRO_FILES)
    condition = 'all(not(target_family = "wasm"), not(target_arch = "riscv32"))'
    source = (crate / "src" / "macros.rs").read_text()
    assert f'#[cfg_attr({condition}, amaru_freeze::trace_fn(in = "Fixed"))]\n            fn of_fixed' in source
    # `friend` has the agent for every target, `demo` only under the condition: the attribute
    # of an exported macro's body would not mean the same in both.
    assert "() => { pub fn from_shared() {} };" in source


def test_macros_are_left_alone_without_a_list_of_files_or_a_check(tmp_path):
    crate = make_macro_tree(tmp_path)
    report = {}
    assert instrument.add_tracing(tmp_path, ["demo"], macros=report) == {"demo": 9}
    assert report == {"files": {}, "reverted": {}, "functions": 0}
    assert "{ fn $name() {} fn fixed() {} }" in (crate / "src" / "lib.rs").read_text()
    assert (crate / "src" / "macros.rs").read_text().count("trace_fn") == 2  # `plain` and `already`


def test_a_listed_file_alone_has_its_macros_marked(tmp_path):
    crate = make_macro_tree(tmp_path)
    assert instrument.macro_safe_files(tmp_path, ["demo"]) == set()

    listing = tmp_path / "recipe" / "trace-macros.json"
    listing.parent.mkdir()
    (listing.parent / "manifest.json").write_text("{}")
    instrument.save_macro_files(listing, {"files": {"crates/demo/src/lib.rs": 2, "crates/friend/src/lib.rs": 1}})
    assert listing.read_text() == (
        '{\n  "files": [\n    "crates/demo/src/lib.rs",\n    "crates/friend/src/lib.rs"\n  ],\n  "functions": 3\n}\n'
    )
    for given in (listing, listing.parent, listing.parent / "manifest.json"):
        assert instrument.macro_safe_files(tmp_path, ["demo"], given) == {"crates/demo/src/lib.rs"}

    # The same list at the root of the tree is found without being asked for.
    (tmp_path / "trace-macros.json").write_text(listing.read_text())
    report = {}
    assert instrument.add_tracing(tmp_path, ["demo"], macros=report) == {"demo": 11}
    assert report["files"] == {"crates/demo/src/lib.rs": 2}
    assert "#[amaru_freeze::trace_fn] fn fixed()" in (crate / "src" / "lib.rs").read_text()
    assert "{ fn second_rule() { fn nested() {} } }" in (crate / "src" / "macros.rs").read_text()

    (tmp_path / "trace-macros.json").write_text('{"files": "all"}')
    try:
        instrument.macro_safe_files(tmp_path, ["demo"])
    except instrument.InstrumentError as error:
        assert "must be a list" in str(error)
    else:
        raise AssertionError("a list that is not one was accepted")


def test_the_command_line_takes_the_list_of_files(tmp_path, capsys):
    crate = make_macro_tree(tmp_path)
    listing = tmp_path / "trace-macros.json"
    listing.write_text('{"files": ["crates/demo/src/lib.rs"], "functions": 2}')
    arguments = ["trace", "--source", str(tmp_path), "--crate", "demo", "--macros", str(listing)]
    assert instrument.main(arguments + ["--dry-run"]) == 0
    assert '"demo": 11' in capsys.readouterr().out
    assert (crate / "src" / "lib.rs").read_text() == SOURCE
    assert instrument.main(arguments) == 0
    assert '"in_macros": {"crates/demo/src/lib.rs": 2}' in capsys.readouterr().out
    assert "#[amaru_freeze::trace_fn] fn fixed()" in (crate / "src" / "lib.rs").read_text()


def checker(root, verdict):
    """A `check` that asks `verdict(marked)` for the compiler's output, given the files whose
    macros are marked; no output means the tree compiles."""
    calls = []

    def check(tree):
        assert tree == root
        marked = {
            path.relative_to(tree).as_posix()
            for path in tree.rglob("*.rs")
            if "trace_fn] fn" in path.read_text() or "trace_fn]\n        $vis" in path.read_text()
        }
        calls.append(marked)
        output = verdict(marked)
        return not output, output

    return check, calls


def test_a_file_the_check_blames_goes_back_to_its_plain_marking(tmp_path):
    (tmp_path / "plain").mkdir()
    plain_crate = make_macro_tree(tmp_path / "plain")
    instrument.add_tracing(tmp_path / "plain", ["demo", "friend"])
    tmp_path = tmp_path / "checked"
    tmp_path.mkdir()
    crate = make_macro_tree(tmp_path)

    def verdict(marked):
        if "crates/demo/src/macros.rs" in marked:
            return (
                "warning: unused variable\n"
                "crates/demo/src/lib.rs:3:1: warning: unused import\n"
                "crates/demo/src/macros.rs:3:9: error[E0433]: failed to resolve: use of unresolved module\n"
                "error: could not compile `demo` (lib) due to 1 previous error\n"
            )
        return ""

    check, calls = checker(tmp_path, verdict)
    report = {}
    counts = instrument.add_tracing(tmp_path, ["demo", "friend"], check=check, macros=report)

    assert (crate / "src" / "macros.rs").read_text() == (plain_crate / "src" / "macros.rs").read_text()
    assert "#[amaru_freeze::trace_fn] fn fixed()" in (crate / "src" / "lib.rs").read_text()
    assert report == {
        "files": {"crates/demo/src/lib.rs": 2},
        "reverted": {
            "crates/demo/src/macros.rs": "crates/demo/src/macros.rs:3:9: error[E0433]: failed to resolve: "
            "use of unresolved module"
        },
        "functions": 2,
    }
    assert counts == {"demo": 11, "friend": 1}
    # Before any macro, with the macros of `demo`, and again without the file blamed.
    assert calls == [set(), {"crates/demo/src/macros.rs", "crates/demo/src/lib.rs"}, {"crates/demo/src/lib.rs"}]

    # A second checked run finds the same file again and puts it back again.
    assert instrument.add_tracing(tmp_path, ["demo", "friend"], check=check) == {"demo": 0, "friend": 0}
    assert (crate / "src" / "macros.rs").read_text() == (plain_crate / "src" / "macros.rs").read_text()


def test_the_usual_output_of_the_compiler_is_understood_too(tmp_path):
    crate = make_macro_tree(tmp_path)
    output = (
        "warning: unused import: `x`\n"
        " --> crates/demo/src/items.rs:1:5\n"
        "  |\n"
        "error[E0433]: failed to resolve: use of unresolved module or unlinked crate `amaru_freeze`\n"
        "   --> /src/crates/demo/src/lib.rs:31:10\n"
        "    |\n"
        " 31 |     ($name:ident) => { fn $name() {} };\n"
        "    |\n"
        "   ::: src/macros.rs:40:1\n"
    )
    assert instrument.error_files(output) == {
        "/src/crates/demo/src/lib.rs": "error[E0433]: failed to resolve: use of unresolved module or unlinked "
        "crate `amaru_freeze`",
        "src/macros.rs": "error[E0433]: failed to resolve: use of unresolved module or unlinked crate `amaru_freeze`",
    }
    check, calls = checker(tmp_path, lambda marked: output if marked else "")
    report = {}
    assert instrument.add_tracing(tmp_path, ["demo"], check=check, macros=report) == {"demo": 9}
    assert sorted(report["reverted"]) == ["crates/demo/src/lib.rs", "crates/demo/src/macros.rs"]
    assert report["files"] == {} and calls[-1] == set()
    assert "{ fn $name() {} fn fixed() {} }" in (crate / "src" / "lib.rs").read_text()


def test_an_error_outside_the_files_marked_stops_the_run(tmp_path):
    crate = make_macro_tree(tmp_path)
    for verdict, message in (
        (lambda marked: "crates/stranger/src/lib.rs:1:1: error: boom\n" if marked else "", "outside the files marked"),
        (lambda marked: "crates/demo/src/lib.rs:1:1: error: boom\n", "before any macro is marked"),
    ):
        check, calls = checker(tmp_path, verdict)
        try:
            instrument.add_tracing(tmp_path, ["demo"], check=check)
        except instrument.InstrumentError as error:
            assert message in str(error) and "boom" in str(error)
        else:
            raise AssertionError("the run went on")
        # The plain marking stays, the macros are as they were.
        assert "{ fn $name() {} fn fixed() {} }" in (crate / "src" / "lib.rs").read_text()
        assert "#[amaru_freeze::trace_fn]\nfn text()" in (crate / "src" / "lib.rs").read_text()
        assert "{ fn second_rule() { fn nested() {} } }" in (crate / "src" / "macros.rs").read_text()
