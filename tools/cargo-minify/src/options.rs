//! Command line parsing, reproducing the behaviour and messages of `gumdrop`.

use std::{fmt, str::FromStr};

use crate::{diff_format::ColorMode, unused::UnusedDiagnosticKind};

pub const USAGE: &str = "\
Optional arguments:
  -q, --quiet           No output printed to stdout
  -p, --package SPEC    Package to minify
  --workspace           Minify all packages in the workspace
  --exclude SPEC        Exclude packages from the minify
  -f, --file SPEC       File to minify
  -i, --ignore SPEC     Ignore files from the minify
  -k, --kinds < FUNCTION | CONST | STATIC | STRUCT | ENUM | UNION | TYPE_ALIAS | ASSOCIATED_FUNCTION | MACRO_DEFINITION >
                        specify which kinds of diagnostics to apply (all by default)
  --apply               Apply changes instead of outputting a diff
  -h, --help            Print help message
  --color WHEN          Coloring: auto, always, never
  --manifest-path PATH  Path to Cargo.toml
  --allow-dirty         Fix code even if the working directory is dirty
  --allow-staged        Fix code even if there are staged files in the VCS
  --allow-no-vcs        Also operate if no version control system was found";

#[derive(Debug, Default)]
pub struct MinifyOptions {
    pub quiet: bool,
    pub package: Vec<String>,
    pub workspace: bool,
    pub exclude: Vec<String>,
    pub file: Vec<String>,
    pub ignore: Vec<String>,
    pub kinds: Vec<UnusedDiagnosticKind>,
    pub apply: bool,
    pub help: bool,
    pub color: ColorMode,
    pub manifest_path: Option<String>,
    pub allow_dirty: bool,
    pub allow_staged: bool,
    pub allow_no_vcs: bool,
}

#[derive(Debug)]
pub enum OptionsError {
    FailedParse(String, String),
    MissingArgument(String),
    UnexpectedArgument(String),
    UnexpectedFree(String),
    UnrecognizedOption(String),
}

impl fmt::Display for OptionsError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            Self::FailedParse(opt, err) => write!(f, "invalid argument to option `{opt}`: {err}"),
            Self::MissingArgument(opt) => write!(f, "missing argument to option `{opt}`"),
            Self::UnexpectedArgument(opt) => {
                write!(f, "option `{opt}` does not accept an argument")
            }
            Self::UnexpectedFree(arg) => write!(f, "unexpected free argument `{arg}`"),
            Self::UnrecognizedOption(opt) => write!(f, "unrecognized option `{opt}`"),
        }
    }
}

impl std::error::Error for OptionsError {}

enum Opt<'a> {
    Short(char),
    Long(&'a str),
    LongWithArg(&'a str, &'a str),
    Free(&'a str),
}

impl Opt<'_> {
    fn name(&self) -> String {
        match self {
            Opt::Short(c) => format!("-{c}"),
            Opt::Long(s) | Opt::LongWithArg(s, _) => format!("--{s}"),
            Opt::Free(_) => "free".to_owned(),
        }
    }
}

struct Parser<'a, S> {
    args: std::slice::Iter<'a, S>,
    shorts: Option<std::str::Chars<'a>>,
}

impl<'a, S: AsRef<str>> Parser<'a, S> {
    fn next_opt(&mut self) -> Option<Opt<'a>> {
        if let Some(mut shorts) = self.shorts.take() {
            if let Some(c) = shorts.next() {
                self.shorts = Some(shorts);
                return Some(Opt::Short(c));
            }
        }
        match self.args.next().map(AsRef::as_ref) {
            Some(arg @ "-") => Some(Opt::Free(arg)),
            // Any argument after `--` is free, and the first free argument is an error
            Some("--") => self.args.next().map(|s| Opt::Free(s.as_ref())),
            Some(long) if long.starts_with("--") => Some(match long.find('=') {
                Some(pos) => Opt::LongWithArg(&long[2..pos], &long[pos + 1..]),
                None => Opt::Long(&long[2..]),
            }),
            Some(short) if short.starts_with('-') => {
                let mut chars = short[1..].chars();
                let opt = chars.next().map(Opt::Short);
                self.shorts = Some(chars);
                opt
            }
            Some(free) => Some(Opt::Free(free)),
            None => None,
        }
    }

    /// The rest of a group of short options, or else the next argument
    fn next_arg(&mut self) -> Option<&'a str> {
        if let Some(rest) = self.shorts.take().map(|s| s.as_str()) {
            if !rest.is_empty() {
                return Some(rest);
            }
        }
        self.args.next().map(AsRef::as_ref)
    }
}

impl MinifyOptions {
    pub fn parse_args<S: AsRef<str>>(args: &[S]) -> Result<Self, OptionsError> {
        let mut parser = Parser {
            args: args.iter(),
            shorts: None,
        };
        let mut opts = Self::default();

        while let Some(opt) = parser.next_opt() {
            let (name, inline) = match opt {
                Opt::Short(c) => (short_to_long(c).unwrap_or(""), None),
                Opt::Long(s) => (s, None),
                Opt::LongWithArg(s, arg) => (s, Some(arg)),
                Opt::Free(arg) => return Err(OptionsError::UnexpectedFree(arg.to_owned())),
            };

            let flag = match name {
                "quiet" => Some(&mut opts.quiet),
                "workspace" => Some(&mut opts.workspace),
                "apply" => Some(&mut opts.apply),
                "help" => Some(&mut opts.help),
                "allow-dirty" => Some(&mut opts.allow_dirty),
                "allow-staged" => Some(&mut opts.allow_staged),
                "allow-no-vcs" => Some(&mut opts.allow_no_vcs),
                _ => None,
            };
            if let Some(flag) = flag {
                if inline.is_some() {
                    return Err(OptionsError::UnexpectedArgument(opt.name()));
                }
                *flag = true;
                continue;
            }

            if !matches!(
                name,
                "package" | "exclude" | "file" | "ignore" | "kinds" | "color" | "manifest-path"
            ) {
                return Err(OptionsError::UnrecognizedOption(opt.name()));
            }
            let arg = match inline {
                Some(arg) => arg,
                None => parser
                    .next_arg()
                    .ok_or_else(|| OptionsError::MissingArgument(opt.name()))?,
            };
            match name {
                "package" => opts.package.push(arg.to_owned()),
                "exclude" => opts.exclude.push(arg.to_owned()),
                "file" => opts.file.push(arg.to_owned()),
                "ignore" => opts.ignore.push(arg.to_owned()),
                "kinds" => opts.kinds.push(parse(&opt, arg)?),
                "color" => opts.color = parse(&opt, arg)?,
                _ => opts.manifest_path = Some(arg.to_owned()),
            }
        }

        Ok(opts)
    }
}

fn short_to_long(c: char) -> Option<&'static str> {
    Some(match c {
        'q' => "quiet",
        'p' => "package",
        'f' => "file",
        'i' => "ignore",
        'k' => "kinds",
        'h' => "help",
        _ => return None,
    })
}

fn parse<T: FromStr>(opt: &Opt, arg: &str) -> Result<T, OptionsError>
where
    T::Err: fmt::Display,
{
    arg.parse()
        .map_err(|e: T::Err| OptionsError::FailedParse(opt.name(), e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> MinifyOptions {
        MinifyOptions::parse_args(args).unwrap()
    }

    fn error(args: &[&str]) -> String {
        MinifyOptions::parse_args(args).unwrap_err().to_string()
    }

    #[test]
    fn defaults_without_arguments() {
        let opts = parse(&[]);
        assert!(!(opts.quiet || opts.workspace || opts.apply || opts.help));
        assert!(!(opts.allow_dirty || opts.allow_staged || opts.allow_no_vcs));
        assert!(opts.package.is_empty() && opts.exclude.is_empty() && opts.kinds.is_empty());
        assert!(opts.file.is_empty() && opts.ignore.is_empty() && opts.manifest_path.is_none());
        assert!(matches!(opts.color, ColorMode::Auto));
    }

    #[test]
    fn long_flags() {
        let opts = parse(&[
            "--quiet",
            "--workspace",
            "--apply",
            "--help",
            "--allow-dirty",
            "--allow-staged",
            "--allow-no-vcs",
        ]);
        assert!(opts.quiet && opts.workspace && opts.apply && opts.help);
        assert!(opts.allow_dirty && opts.allow_staged && opts.allow_no_vcs);
    }

    #[test]
    fn short_flags_alone_and_grouped() {
        assert!(parse(&["-q"]).quiet);
        assert!(parse(&["-h"]).help);
        let opts = parse(&["-qh"]);
        assert!(opts.quiet && opts.help);
    }

    #[test]
    fn values_in_every_form_accumulate_in_order() {
        let opts = parse(&[
            "-p",
            "a",
            "-pb",
            "--package",
            "c",
            "--package=d",
            "-qpe",
            "--package=",
        ]);
        assert_eq!(opts.package, ["a", "b", "c", "d", "e", ""]);
        assert!(opts.quiet);
        let opts = parse(&[
            "--exclude",
            "x",
            "-f",
            "f",
            "--file=g",
            "-i",
            "i",
            "--ignore=j",
        ]);
        assert_eq!(
            (opts.exclude, opts.file, opts.ignore),
            (
                vec!["x".into()],
                vec!["f".into(), "g".into()],
                vec!["i".into(), "j".into()]
            )
        );
    }

    #[test]
    fn a_value_may_look_like_an_option() {
        assert_eq!(parse(&["-p", "--apply"]).package, ["--apply"]);
        assert!(!parse(&["-p", "--apply"]).apply);
        assert_eq!(parse(&["--file", "-"]).file, ["-"]);
    }

    #[test]
    fn the_last_single_value_wins() {
        let opts = parse(&["--color=never", "--color", "always", "--manifest-path=a"]);
        assert!(matches!(opts.color, ColorMode::Always));
        let opts = parse(&["--manifest-path=a", "--manifest-path", "b"]);
        assert_eq!(opts.manifest_path.as_deref(), Some("b"));
    }

    #[test]
    fn kinds_parse_case_and_punctuation_insensitively() {
        use UnusedDiagnosticKind::*;
        let opts = parse(&["-kFUNCTION", "-k", "type_alias", "--kinds=Macro-Definition"]);
        assert_eq!(opts.kinds, [Function, TypeAlias, MacroDefinition]);
        let opts = parse(&["-kconstant", "-kstatic", "-kstruct", "-kenum", "-kunion"]);
        assert_eq!(opts.kinds, [Constant, Static, Struct, Enum, Union]);
        let opts = parse(&["-ktype", "-kassociated", "-kASSOCIATED_FUNCTION", "-kmacro"]);
        assert_eq!(
            opts.kinds,
            [
                TypeAlias,
                AssociatedFunction,
                AssociatedFunction,
                MacroDefinition
            ]
        );
    }

    #[test]
    fn unrecognized_options() {
        assert_eq!(error(&["--bogus"]), "unrecognized option `--bogus`");
        assert_eq!(error(&["--bogus=1"]), "unrecognized option `--bogus`");
        assert_eq!(error(&["-z"]), "unrecognized option `-z`");
        assert_eq!(error(&["-qz"]), "unrecognized option `-z`");
        assert_eq!(error(&["-q=1"]), "unrecognized option `-=`");
        assert_eq!(error(&["--=x"]), "unrecognized option `--`");
        assert_eq!(error(&["---"]), "unrecognized option `---`");
        assert_eq!(error(&["--Quiet"]), "unrecognized option `--Quiet`");
        assert_eq!(
            error(&["--allow_dirty"]),
            "unrecognized option `--allow_dirty`"
        );
        assert_eq!(error(&["-w"]), "unrecognized option `-w`");
        assert_eq!(error(&["-ä"]), "unrecognized option `-ä`");
    }

    #[test]
    fn missing_arguments() {
        assert_eq!(error(&["--file"]), "missing argument to option `--file`");
        assert_eq!(error(&["-k"]), "missing argument to option `-k`");
        assert_eq!(error(&["-qp"]), "missing argument to option `-p`");
        assert_eq!(
            error(&["--manifest-path"]),
            "missing argument to option `--manifest-path`"
        );
    }

    #[test]
    fn flags_reject_inline_arguments() {
        assert_eq!(
            error(&["--help=x"]),
            "option `--help` does not accept an argument"
        );
        assert_eq!(
            error(&["--quiet="]),
            "option `--quiet` does not accept an argument"
        );
        let err = error(&["--allow-dirty=yes"]);
        assert_eq!(err, "option `--allow-dirty` does not accept an argument");
    }

    #[test]
    fn free_arguments_are_rejected() {
        assert_eq!(error(&["foo"]), "unexpected free argument `foo`");
        assert_eq!(error(&["-"]), "unexpected free argument `-`");
        assert_eq!(error(&[""]), "unexpected free argument ``");
        assert_eq!(
            error(&["--", "--apply"]),
            "unexpected free argument `--apply`"
        );
        assert!(parse(&["--apply", "--"]).apply);
        assert_eq!(error(&["--", "--"]), "unexpected free argument `--`");
        assert_eq!(
            error(&["-q", "--", "-q", "x"]),
            "unexpected free argument `-q`"
        );
    }

    #[test]
    fn invalid_values() {
        let err = error(&["--color=bad"]);
        assert_eq!(err, "invalid argument to option `--color`: unsupported color mode, pick any of: auto, always, never");
        assert_eq!(
            error(&["-kbogus"]),
            "invalid argument to option `-k`: not an unused-diagnostic"
        );
        assert_eq!(
            error(&["--kinds="]),
            "invalid argument to option `--kinds`: not an unused-diagnostic"
        );
    }

    #[test]
    fn every_kind_in_the_usage_parses() {
        use UnusedDiagnosticKind::*;
        let line = USAGE.lines().find(|line| line.contains("--kinds")).unwrap();
        let listed = line.split_once('<').unwrap().1.trim_end_matches('>');
        let kinds: Vec<_> = listed.split('|').map(str::trim).collect();
        let args: Vec<_> = kinds.iter().map(|kind| format!("-k{kind}")).collect();
        let parsed = parse(&args.iter().map(String::as_str).collect::<Vec<_>>()).kinds;
        let all = [
            Function,
            Constant,
            Static,
            Struct,
            Enum,
            Union,
            TypeAlias,
            AssociatedFunction,
            MacroDefinition,
        ];
        assert_eq!(parsed, all, "{kinds:?}");
    }

    #[test]
    fn the_first_error_wins() {
        assert_eq!(error(&["--bogus", "-z"]), "unrecognized option `--bogus`");
        assert_eq!(error(&["foo", "--bogus"]), "unexpected free argument `foo`");
    }

    #[test]
    fn every_option_in_the_usage_is_parsed() {
        let mut longs = vec![];
        for line in USAGE.lines().filter(|line| line.starts_with("  -")) {
            let words: Vec<_> = line.split_whitespace().collect();
            let long = words.iter().find(|w| w.starts_with("--")).unwrap();
            let long = long.trim_end_matches(',');
            let value = match words.get(words.iter().position(|w| w.starts_with("--")).unwrap() + 1)
            {
                Some(&"SPEC") | Some(&"PATH") => Some("x"),
                Some(&"WHEN") => Some("never"),
                Some(&"<") => Some("FUNCTION"),
                _ => None,
            };
            let mut args = vec![long];
            args.extend(value);
            let opts = parse(&args);
            assert_ne!(
                format!("{opts:?}"),
                format!("{:?}", MinifyOptions::default()),
                "{long}"
            );
            if let Some(short) = words[0].strip_suffix(',') {
                let mut args = vec![short];
                args.extend(value);
                assert_eq!(
                    format!("{:?}", parse(&args)),
                    format!("{opts:?}"),
                    "{short}"
                );
            }
            longs.push(long);
        }
        assert_eq!(longs.len(), 14);
    }
}
