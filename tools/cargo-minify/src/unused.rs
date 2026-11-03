use std::{
    collections::HashSet,
    fmt::{Display, Formatter},
    io::BufReader,
    path::Path,
    process::{Command, Stdio},
    str::FromStr,
};

use cargo_metadata::{
    diagnostic::{Diagnostic, DiagnosticSpan},
    Message,
};

use crate::{cauterize, error::Result, resolver, CrateResolutionOptions, FileResolutionOptions};

/// Only the library target is minified. It is checked twice, once as is and
/// once with `cfg(test)`, and an item counts as unused only if both builds
/// agree: code used solely by unit tests, or solely outside them, is kept.
pub fn get_unused(
    manifest_path: Option<&Path>,
    crate_resolution: &CrateResolutionOptions,
    file_resolution: &FileResolutionOptions,
    kinds: &[UnusedDiagnosticKind],
) -> Result<impl Iterator<Item = UnusedDiagnostic>> {
    let targets = resolver::get_targets(manifest_path, crate_resolution)?;
    let check = |profile: &str| -> Result<Vec<UnusedDiagnostic>> {
        let mut command = Command::new("cargo");

        command.args(["check", "--lib", "--profile", profile]);
        command.args(["--quiet", "--message-format", "json"]);

        match crate_resolution {
            CrateResolutionOptions::Root => {}
            CrateResolutionOptions::Workspace { exclude } => {
                command.arg("--workspace");

                for package in *exclude {
                    command.args(["--exclude", package]);
                }
            }
            CrateResolutionOptions::Package { packages } => {
                for package in *packages {
                    command.args(["-p", package]);
                }
            }
        }

        let mut child = command.stdout(Stdio::piped()).spawn()?;
        let stdout = child.stdout.take().unwrap();
        let reader = BufReader::new(stdout);

        let unused = Message::parse_stream(reader)
            .flatten()
            .filter_map(|message| {
                if let Message::CompilerMessage(message) = message {
                    Some(message)
                } else {
                    None
                }
            })
            .filter(|message| targets.contains(&message.target))
            .map(|message| message.message)
            .filter_map(|diagnostic| UnusedDiagnostic::try_from(diagnostic).ok())
            // Ignore unused warnings originating from macro expansions
            .filter(|diagnostic| diagnostic.span.expansion.is_none())
            .collect();
        child.wait()?;

        Ok(unused)
    };

    let in_tests: HashSet<_> = check("test")?.iter().map(UnusedDiagnostic::key).collect();
    let (unused, staying): (Vec<_>, Vec<_>) = check("dev")?
        .into_iter()
        .filter(|diagnostic| in_tests.contains(&diagnostic.key()))
        .partition(|diagnostic| {
            (kinds.is_empty() || kinds.contains(&diagnostic.kind))
                && file_resolution.is_included(&diagnostic.span.file_name)
        });

    Ok(spare_what_stays_mentions(unused, &staying).into_iter())
}

/// Leaves out the unused items that unused items left in the code, e.g. those of other kinds or
/// in other files, mention by name, and in turn the ones those mention: else it would not build
fn spare_what_stays_mentions(
    mut unused: Vec<UnusedDiagnostic>,
    staying: &[UnusedDiagnostic],
) -> Vec<UnusedDiagnostic> {
    let mut mentioned = cauterize::mentioned_by(staying);
    loop {
        let spared: Vec<_>;
        (spared, unused) = unused
            .into_iter()
            .partition(|diagnostic| mentioned.contains(&diagnostic.ident));
        if spared.is_empty() {
            return unused;
        }
        mentioned.extend(cauterize::mentioned_by(&spared));
    }
}

#[derive(Debug)]
pub struct UnusedDiagnostic {
    pub kind: UnusedDiagnosticKind,
    pub ident: String,
    pub span: DiagnosticSpan,
}

impl UnusedDiagnostic {
    fn key(&self) -> (String, usize) {
        (self.span.file_name.clone(), self.span.byte_start as usize)
    }
}

impl TryFrom<Diagnostic> for UnusedDiagnostic {
    type Error = NotUnusedDiagnostic;

    fn try_from(value: Diagnostic) -> Result<Self, Self::Error> {
        let message = value.message;

        let (first, message) = message.split_once(' ').ok_or(NotUnusedDiagnostic)?;
        match UnusedDiagnosticKind::from_str(first) {
            Ok(kind) => {
                let message = match kind {
                    UnusedDiagnosticKind::Constant
                    | UnusedDiagnosticKind::Static
                    | UnusedDiagnosticKind::Function
                    | UnusedDiagnosticKind::Struct
                    | UnusedDiagnosticKind::Enum
                    | UnusedDiagnosticKind::Union => message,
                    UnusedDiagnosticKind::TypeAlias => {
                        let (alias, message) =
                            message.split_once(' ').ok_or(NotUnusedDiagnostic)?;

                        if alias != "alias" {
                            return Err(NotUnusedDiagnostic);
                        }

                        message
                    }
                    UnusedDiagnosticKind::AssociatedFunction => {
                        let (function, message) =
                            message.split_once(' ').ok_or(NotUnusedDiagnostic)?;

                        if function != "function" {
                            return Err(NotUnusedDiagnostic);
                        }

                        message
                    }
                    UnusedDiagnosticKind::MacroDefinition => return Err(NotUnusedDiagnostic),
                };

                let (mut ident, message) = message.split_once(' ').ok_or(NotUnusedDiagnostic)?;
                ident = ident.strip_prefix('`').ok_or(NotUnusedDiagnostic)?;
                ident = ident.strip_suffix('`').ok_or(NotUnusedDiagnostic)?;
                let ident = ident.to_owned();

                let suffix = match kind {
                    UnusedDiagnosticKind::Constant
                    | UnusedDiagnosticKind::Static
                    | UnusedDiagnosticKind::Function
                    | UnusedDiagnosticKind::Enum
                    | UnusedDiagnosticKind::Union
                    | UnusedDiagnosticKind::TypeAlias
                    | UnusedDiagnosticKind::AssociatedFunction => "is never used",
                    UnusedDiagnosticKind::Struct => "is never constructed",
                    UnusedDiagnosticKind::MacroDefinition => return Err(NotUnusedDiagnostic),
                };

                if message != suffix {
                    return Err(NotUnusedDiagnostic);
                }

                let span = value
                    .spans
                    .into_iter()
                    .find(|span| span.is_primary)
                    .ok_or(NotUnusedDiagnostic)?;

                Ok(UnusedDiagnostic { kind, ident, span })
            }
            Err(_) => {
                if first != "unused" {
                    return Err(NotUnusedDiagnostic);
                }

                let (mut kind, message) = message.split_once(' ').ok_or(NotUnusedDiagnostic)?;
                kind = kind.strip_suffix(':').unwrap_or(kind);
                let kind: UnusedDiagnosticKind = kind.parse()?;

                let message = match kind {
                    UnusedDiagnosticKind::Constant
                    | UnusedDiagnosticKind::Static
                    | UnusedDiagnosticKind::Function
                    | UnusedDiagnosticKind::Struct
                    | UnusedDiagnosticKind::Enum
                    | UnusedDiagnosticKind::Union
                    | UnusedDiagnosticKind::TypeAlias
                    | UnusedDiagnosticKind::AssociatedFunction => return Err(NotUnusedDiagnostic),
                    UnusedDiagnosticKind::MacroDefinition => {
                        let (definition, message) =
                            message.split_once(' ').ok_or(NotUnusedDiagnostic)?;

                        if definition != "definition:" {
                            return Err(NotUnusedDiagnostic);
                        }

                        message
                    }
                };

                let mut split = message.splitn(2, ' ');
                let mut ident = split.next().unwrap();
                let message = split.next().unwrap_or_default();

                ident = ident.strip_prefix('`').ok_or(NotUnusedDiagnostic)?;
                ident = ident.strip_suffix('`').ok_or(NotUnusedDiagnostic)?;
                let ident = ident.to_owned();

                if !message.is_empty() {
                    return Err(NotUnusedDiagnostic);
                }

                let span = value
                    .spans
                    .into_iter()
                    .find(|span| span.is_primary)
                    .ok_or(NotUnusedDiagnostic)?;

                Ok(UnusedDiagnostic { kind, ident, span })
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum UnusedDiagnosticKind {
    Constant,
    Static,
    Function,
    Struct,
    Enum,
    Union,
    TypeAlias,
    AssociatedFunction,
    MacroDefinition,
}

impl FromStr for UnusedDiagnosticKind {
    type Err = NotUnusedDiagnostic;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s
            .chars()
            .filter(|c| c.is_alphanumeric())
            .map(|c| c.to_ascii_lowercase())
            .collect::<String>()
            .as_str()
        {
            "const" | "constant" => Ok(UnusedDiagnosticKind::Constant),
            "static" => Ok(UnusedDiagnosticKind::Static),
            "function" => Ok(UnusedDiagnosticKind::Function),
            "struct" => Ok(UnusedDiagnosticKind::Struct),
            "enum" => Ok(UnusedDiagnosticKind::Enum),
            "union" => Ok(UnusedDiagnosticKind::Union),
            "type" | "typealias" => Ok(UnusedDiagnosticKind::TypeAlias),
            "associated" | "associatedfunction" => Ok(UnusedDiagnosticKind::AssociatedFunction),
            "macro" | "macrodefinition" => Ok(UnusedDiagnosticKind::MacroDefinition),
            _ => Err(NotUnusedDiagnostic),
        }
    }
}

#[derive(Debug)]
pub struct NotUnusedDiagnostic;

impl Display for NotUnusedDiagnostic {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        write!(f, "not an unused-diagnostic")
    }
}

impl std::error::Error for NotUnusedDiagnostic {}

#[cfg(test)]
mod tests {
    use super::*;
    use UnusedDiagnosticKind::*;

    /// A warning as `cargo check --message-format json` reports it, whose span may be primary
    fn diagnostic(message: &str, is_primary: bool) -> Diagnostic {
        let span = format!(
            r#"{{"file_name":"src/lib.rs","byte_start":3,"byte_end":4,"line_start":1,"line_end":1,
            "column_start":4,"column_end":5,"is_primary":{is_primary},"text":[],"label":null,
            "suggested_replacement":null,"suggestion_applicability":null,"expansion":null}}"#
        );
        let line = format!(
            r#"{{"reason":"compiler-message","package_id":"p","target":{{"name":"p","kind":["lib"],
            "src_path":"/p/src/lib.rs"}},"message":{{"message":{message:?},"code":null,
            "level":"warning","spans":[{span}],"children":[],"rendered":null}}}}"#
        )
        .replace('\n', "");
        match Message::parse_stream(line.as_bytes())
            .next()
            .unwrap()
            .unwrap()
        {
            Message::CompilerMessage(message) => message.message,
            other => panic!("not a compiler message: {other:?}"),
        }
    }

    fn unused(message: &str) -> Option<(UnusedDiagnosticKind, String)> {
        let diagnostic = UnusedDiagnostic::try_from(diagnostic(message, true)).ok()?;
        assert_eq!(diagnostic.span.byte_start, 3);
        Some((diagnostic.kind, diagnostic.ident))
    }

    #[test]
    fn recognizes_every_unused_item_rustc_reports() {
        for (message, kind, ident) in [
            ("function `f` is never used", Function, "f"),
            ("constant `C` is never used", Constant, "C"),
            ("static `S` is never used", Static, "S"),
            ("struct `S` is never constructed", Struct, "S"),
            ("enum `E` is never used", Enum, "E"),
            ("union `U` is never used", Union, "U"),
            ("type alias `T` is never used", TypeAlias, "T"),
            (
                "associated function `new` is never used",
                AssociatedFunction,
                "new",
            ),
            ("unused macro definition: `m`", MacroDefinition, "m"),
            // rustc names raw identifiers without their `r#`
            ("function `match` is never used", Function, "match"),
        ] {
            assert_eq!(unused(message), Some((kind, ident.to_owned())), "{message}");
        }
    }

    #[test]
    fn ignores_every_other_warning() {
        for message in [
            "unused variable: `x`",
            "unused import: `std::fs`",
            "unused",
            "lonely",
            "",
            "function `f` is never constructed",
            "function `f` is never used!",
            "function `f`",
            "function f is never used",
            "function `f is never used",
            "struct `S` is never used",
            "type `T` is never used",
            "type alias",
            "associated item `x` is never used",
            "associated function",
            "macro definition `m` is never used",
            "unused function `f`",
            "unused macro definitions: `m`",
            "unused macro definition: `m` here",
            "unused macro definition: m",
            "unused macro definition:",
            // rustc groups dead items that are declared together, which upstream leaves alone
            "functions `a` and `b` are never used",
            "associated functions `a` and `b` are never used",
        ] {
            assert_eq!(unused(message), None, "{message}");
        }
    }

    #[test]
    fn needs_a_primary_span() {
        for message in ["function `f` is never used", "unused macro definition: `m`"] {
            assert!(UnusedDiagnostic::try_from(diagnostic(message, false)).is_err());
        }
    }

    #[test]
    fn keys_by_file_and_offset() {
        let diagnostic = UnusedDiagnostic::try_from(diagnostic("enum `E` is never used", true));
        assert_eq!(diagnostic.unwrap().key(), ("src/lib.rs".to_owned(), 3));
    }
}
