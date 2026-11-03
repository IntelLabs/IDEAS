use std::{
    collections::{BTreeMap, HashSet},
    ops::Range,
    path::{Path, PathBuf},
};

use syn::{
    ext::IdentExt,
    spanned::Spanned,
    visit::{self, Visit},
    File,
};

use crate::unused::{UnusedDiagnostic, UnusedDiagnosticKind};

const SPACE: u8 = b' ';
const NEWLINE: u8 = b'\n';
const CARRIAGE_RETURN: u8 = b'\r';

pub struct Change {
    file_name: PathBuf,
    original_content: Vec<u8>,
    proposed_content: Vec<u8>,
}

impl Change {
    pub fn file_name(&self) -> &Path {
        &self.file_name
    }

    pub fn original_content(&self) -> &[u8] {
        &self.original_content
    }

    pub fn proposed_content(&self) -> &[u8] {
        &self.proposed_content
    }
}

/// Finds the position of the first whitespace that is considered belonging
/// to the next definition/declaration (this is a kind of "heuristic")
/// Current heuristic:
/// - leave prefixing whitespace as is (if it doesn't contain a newline)
/// - remove the rest of the line (if there is a newline)
fn find_prefix_whitespace(src: &[u8]) -> usize {
    (0..src.len())
        .rev()
        .take_while(|&k| src[k].is_ascii_whitespace())
        .find(|&k| src[k] == NEWLINE)
        .map(|j| j + 1)
        .unwrap_or(src.len())
}

/// Finds the position of the first whitespace that is not considered belonging
/// to the previous definition/declaration (this is kind of "heuristic")
/// Current heuristic:
/// - if there is a newline, eat all space before it, and the newline
/// - if there is no newline, eat all trailing whitespace until the next token
fn find_suffix_whitespace(src: &[u8]) -> usize {
    src.iter()
        .position(|c| *c != SPACE && *c != CARRIAGE_RETURN)
        .map(|pos| if src[pos] == NEWLINE { pos + 1 } else { pos })
        .unwrap_or(src.len())
}

/// Turns a list of "locations of identifiers" into a list of "chunks"; an
/// identifier with a known byte offset only matches the item declared there
fn diagnostics_to_ranges<'a>(
    src: &'a [u8],
    idents: impl IntoIterator<Item = (UnusedDiagnosticKind, String, Option<usize>)> + 'a,
) -> Result<impl Iterator<Item = Range<usize>> + 'a, syn::Error> {
    let s = String::from_utf8_lossy(src);
    let parsed = syn::parse_str::<syn::File>(&s)?;

    let cumulative_lengths = line_offsets(src);
    let mut items = Items::default();
    items.visit_file(&parsed);

    let ranges: Vec<_> = idents
        .into_iter()
        .filter_map(|(kind, ident, at)| {
            items.0.iter().find_map(|(item_kind, item_ident, span)| {
                let start = || to_offset(&s, &cumulative_lengths, item_ident.span().start());
                (*item_kind == kind
                    && item_ident.unraw() == ident
                    && at.is_none_or(|at| start() == at))
                .then(|| to_range(&s, &cumulative_lengths, *span))
            })
        })
        .collect();

    Ok(ranges.into_iter())
}

/// Every identifier in the items `diagnostics` point at, e.g. the functions they call
pub fn mentioned_by(diagnostics: &[UnusedDiagnostic]) -> HashSet<String> {
    let mut by_file = BTreeMap::<_, Vec<_>>::new();
    for diagnostic in diagnostics {
        let at = diagnostic.span.byte_start as usize;
        let file = by_file.entry(&diagnostic.span.file_name).or_default();
        file.push((diagnostic.kind, diagnostic.ident.clone(), Some(at)));
    }

    let mut mentioned = HashSet::new();
    for (file_name, items) in by_file {
        let Ok(src) = std::fs::read(file_name) else {
            continue;
        };
        let Ok(ranges) = diagnostics_to_ranges(&src, items) else {
            continue;
        };
        for range in ranges {
            let item = String::from_utf8_lossy(&src[range]);
            if let Ok(tokens) = item.parse() {
                collect_idents(tokens, &mut mentioned);
            }
        }
    }
    mentioned
}

fn collect_idents(tokens: proc_macro2::TokenStream, idents: &mut HashSet<String>) {
    for token in tokens {
        match token {
            proc_macro2::TokenTree::Ident(ident) => {
                idents.insert(ident.unraw().to_string());
            }
            proc_macro2::TokenTree::Group(group) => collect_idents(group.stream(), idents),
            _ => {}
        }
    }
}

/// Every removable item in a file, wherever it is nested, in source order
#[derive(Default)]
struct Items<'ast>(Vec<(UnusedDiagnosticKind, &'ast syn::Ident, proc_macro2::Span)>);

impl<'ast> Visit<'ast> for Items<'ast> {
    fn visit_item(&mut self, item: &'ast syn::Item) {
        use syn::Item;
        use UnusedDiagnosticKind::*;
        let found = match item {
            Item::Const(obj) => Some((Constant, &obj.ident)),
            Item::Enum(obj) => Some((Enum, &obj.ident)),
            Item::Fn(obj) => Some((Function, &obj.sig.ident)),
            Item::Macro(syn::ItemMacro {
                ident: Some(name), ..
            }) => Some((MacroDefinition, name)),
            Item::Static(obj) => Some((Static, &obj.ident)),
            Item::Struct(obj) => Some((Struct, &obj.ident)),
            Item::Type(obj) => Some((TypeAlias, &obj.ident)),
            Item::Union(obj) => Some((Union, &obj.ident)),
            _ => None,
        };
        if let Some((kind, ident)) = found {
            self.0.push((kind, ident, item.span()));
        }
        visit::visit_item(self, item);
    }

    fn visit_foreign_item(&mut self, item: &'ast syn::ForeignItem) {
        use syn::ForeignItem;
        use UnusedDiagnosticKind::*;
        let found = match item {
            ForeignItem::Fn(obj) => Some((Function, &obj.sig.ident)),
            ForeignItem::Static(obj) => Some((Static, &obj.ident)),
            ForeignItem::Type(obj) => Some((TypeAlias, &obj.ident)),
            _ => None,
        };
        if let Some((kind, ident)) = found {
            self.0.push((kind, ident, item.span()));
        }
        visit::visit_foreign_item(self, item);
    }

    fn visit_impl_item(&mut self, item: &'ast syn::ImplItem) {
        use syn::ImplItem;
        use UnusedDiagnosticKind::*;
        let found = match item {
            ImplItem::Const(obj) => Some((Constant, &obj.ident)),
            ImplItem::Fn(obj) => Some((AssociatedFunction, &obj.sig.ident)),
            ImplItem::Type(obj) => Some((TypeAlias, &obj.ident)),
            _ => None,
        };
        if let Some((kind, ident)) = found {
            self.0.push((kind, ident, item.span()));
        }
        visit::visit_impl_item(self, item);
    }
}

fn expand_ranges_to_include_whitespace<'a>(
    src: &'a [u8],
    iter: impl Iterator<Item = Range<usize>> + 'a,
) -> impl Iterator<Item = Range<usize>> + 'a {
    iter.map(|range| {
        find_prefix_whitespace(&src[..range.start])
            ..find_suffix_whitespace(&src[range.end..]) + range.end
    })
}

/// Deletes a list-of-positions-of-identifiers from a bytearray that is valid
/// rust code BUGS: if the position is in the body of a function, it will try to
/// delete identifiers there ...  probably?
pub fn delete_chunks(src: &[u8], chunks_to_delete: &[Range<usize>]) -> Vec<u8> {
    src.iter()
        .enumerate()
        .filter_map(|(i, &byte)| {
            if chunks_to_delete.iter().any(|range| range.contains(&i)) {
                None
            } else {
                Some(byte)
            }
        })
        .collect()
}

/// Deletes a list-of-positions-of-identifiers from a bytearray that is valid
/// rust code BUGS: if the position is in the body of a function, it will try to
/// delete identifiers there ...  probably?
pub fn rust_delete(
    src: &[u8],
    diagnostics: impl IntoIterator<Item = (UnusedDiagnosticKind, String, Option<usize>)>,
) -> Result<Vec<u8>, syn::Error> {
    let chunks_to_delete =
        expand_ranges_to_include_whitespace(src, diagnostics_to_ranges(src, diagnostics)?);

    Ok(delete_chunks(src, &chunks_to_delete.collect::<Vec<_>>()))
}

/// Processes a list of file+list-of-edits into an iterator of
/// filenames+proposed new contents
fn process_files<Iter: IntoIterator<Item = UnusedDiagnostic>>(
    diagnostics: impl IntoIterator<Item = (PathBuf, Iter)>,
) -> impl Iterator<Item = Change> {
    diagnostics
        .into_iter()
        .filter_map(|(file_name, diagnostic)| {
            let original_content = std::fs::read(&file_name).ok()?;
            let removed_unused = rust_delete(
                &original_content,
                diagnostic.into_iter().map(|warn| {
                    let at = warn.span.byte_start as usize;
                    (warn.kind, warn.ident, Some(at))
                }),
            )
            .expect("syntax error");
            let proposed_content = remove_empty_blocks(&removed_unused).expect("syntax error");

            let change = Change {
                file_name,
                original_content,
                proposed_content,
            };

            Some(change)
        })
}

/// Process a list of UnusedDiagnostics into an iterator of filenames+proposed contents
pub fn process_diagnostics(
    diagnostics: impl IntoIterator<Item = UnusedDiagnostic>,
) -> impl Iterator<Item = Change> {
    let mut by_file = BTreeMap::<_, Vec<_>>::new();
    for diagnostic in diagnostics {
        let path = PathBuf::from(&diagnostic.span.file_name);
        by_file.entry(path).or_default().push(diagnostic);
    }
    process_files(by_file)
}

/// Create a table of byte locations of newline symbols,
/// to translate LineColumn's into exact offsets
fn line_offsets(bytes: &[u8]) -> Vec<usize> {
    let mut offsets: Vec<usize> = bytes
        .iter()
        .enumerate()
        .filter_map(|(pos, b)| match b {
            // TODO: Support \r\n
            b'\n' => Some(pos + 1),
            _ => None,
        })
        .collect();
    // First line has no offset
    offsets.insert(0, 0);

    offsets
}

/// Columns count characters, not bytes
fn to_offset(src: &str, offsets: &[usize], pos: proc_macro2::LineColumn) -> usize {
    let line = offsets[pos.line - 1];
    let column: usize = src[line..]
        .chars()
        .take(pos.column)
        .map(char::len_utf8)
        .sum();

    line + column
}

fn to_range(src: &str, offsets: &[usize], span: proc_macro2::Span) -> Range<usize> {
    to_offset(src, offsets, span.start())..to_offset(src, offsets, span.end())
}

fn remove_empty_blocks(bytes: &[u8]) -> Result<Vec<u8>, syn::Error> {
    let s = String::from_utf8_lossy(bytes).to_string();
    let ast: File = syn::parse_str(&s)?;

    let cumulative_lengths = line_offsets(bytes);

    let spans = ast
        .items
        .iter()
        .filter_map(|item| match item {
            syn::Item::ForeignMod(block) => {
                (block.items.is_empty() && block.attrs.is_empty()).then(|| block.span())
            }
            syn::Item::Impl(block) => {
                (block.items.is_empty() && block.attrs.is_empty() && block.trait_.is_none())
                    .then(|| block.span())
            }
            _ => None,
        })
        .map(|span| to_range(&s, &cumulative_lengths, span));

    let expanded_spans: Vec<Range<usize>> =
        expand_ranges_to_include_whitespace(bytes, spans).collect();

    Ok(delete_chunks(bytes, &expanded_spans))
}

/// This actually applies a collection of changes to your filesystem (use with care)
pub fn commit_changes(
    changes: impl IntoIterator<Item = Change>,
) -> Result<(), Vec<std::io::Error>> {
    let errors = changes
        .into_iter()
        .filter_map(|change| std::fs::write(change.file_name, change.proposed_content).err())
        .collect::<Vec<_>>();

    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors)
    }
}

#[cfg(test)]
mod test {
    use super::*;

    fn fun(name: &str) -> (UnusedDiagnosticKind, String, Option<usize>) {
        (UnusedDiagnosticKind::Function, name.to_owned(), None)
    }

    fn constant(name: &str) -> (UnusedDiagnosticKind, String, Option<usize>) {
        (UnusedDiagnosticKind::Constant, name.to_owned(), None)
    }

    #[test]
    fn identifier_to_span() {
        let src = b"fn foo() {}  fn foa() -> i32 { barf; } const FOO: i32 = 42;";
        //          01234567890123456789012345678901234567890123456789012345678
        //                    1         2         3         4         5
        let pos = diagnostics_to_ranges(src, [fun("foo"), fun("foa"), constant("FOO")])
            .unwrap()
            .collect::<Vec<_>>();
        assert_eq!(pos, vec![0..11, 13..38, 39..59]);
    }

    #[allow(clippy::single_range_in_vec_init)]
    #[test]
    fn chunk_deletion() {
        let src = b"fn foo() {}  fn foa() -> i32 { barf; } const FOO: i32 = 42;";
        //          012345678901234567890123456789012345678901234567890123456
        assert_eq!(
            delete_chunks(src, &[5..8]),
            b"fn fo {}  fn foa() -> i32 { barf; } const FOO: i32 = 42;"
        );
    }

    #[test]
    fn deletion() {
        let src = b"fn foo() { }fn foa() -> i32 { barf; }const FOO: i32 = 42;";
        assert_eq!(
            rust_delete(src, [fun("foo")]).unwrap(),
            b"fn foa() -> i32 { barf; }const FOO: i32 = 42;"
        );
        assert_eq!(
            rust_delete(src, [fun("foa")]).unwrap(),
            b"fn foo() { }const FOO: i32 = 42;"
        );
        assert_eq!(
            rust_delete(src, [constant("FOO")]).unwrap(),
            b"fn foo() { }fn foa() -> i32 { barf; }"
        );
    }

    #[test]
    fn type_check() {
        let src = b"fn foo() { }fn foa() -> i32 { barf; }const FOO: i32 = 42;";
        assert_eq!(
            rust_delete(src, [constant("foo")]).unwrap(),
            b"fn foo() { }fn foa() -> i32 { barf; }const FOO: i32 = 42;"
        );
        assert_eq!(
            rust_delete(src, [constant("foa")]).unwrap(),
            b"fn foo() { }fn foa() -> i32 { barf; }const FOO: i32 = 42;"
        );
        assert_eq!(
            rust_delete(src, [fun("FOO")]).unwrap(),
            b"fn foo() { }fn foa() -> i32 { barf; }const FOO: i32 = 42;"
        );
    }

    #[test]
    fn formatting_preserval() {
        let src = b" fn foo(){}  fn foa()  -> huk {  barf; }   const FOO: i32 = 42;  fn bar(){ } ";
        assert_eq!(
            rust_delete(src, [fun("foo")]).unwrap(),
            b" fn foa()  -> huk {  barf; }   const FOO: i32 = 42;  fn bar(){ } "
        );
        assert_eq!(
            rust_delete(src, [fun("foa")]).unwrap(),
            b" fn foo(){}  const FOO: i32 = 42;  fn bar(){ } "
        );
        assert_eq!(
            rust_delete(src, [constant("FOO")]).unwrap(),
            b" fn foo(){}  fn foa()  -> huk {  barf; }   fn bar(){ } "
        );
        assert_eq!(
            rust_delete(src, [fun("bar")]).unwrap(),
            b" fn foo(){}  fn foa()  -> huk {  barf; }   const FOO: i32 = 42;  "
        );

        assert_eq!(
            rust_delete(src, [fun("foa"), fun("foo")]).unwrap(),
            b" const FOO: i32 = 42;  fn bar(){ } "
        );
        assert_eq!(
            rust_delete(src, [fun("foa"), constant("FOO")]).unwrap(),
            b" fn foo(){}  fn bar(){ } "
        );
    }

    #[test]
    #[rustfmt::skip]
    fn whitespace_semi_preserval() {
        let src = b" fn foo() {} fn fixme() {} fn main() {}";
        assert_eq!(
            rust_delete(src, [fun("fixme")]).unwrap(),
            b" fn foo() {} fn main() {}"
        );
        let src = b" fn foo() {} fn fixme() {}fn main() {}";
        assert_eq!(
            rust_delete(src, [fun("fixme")]).unwrap(),
            b" fn foo() {} fn main() {}"
        );
        let src = b" fn foo() {}fn fixme() {} fn main() {}";
        assert_eq!(
            rust_delete(src, [fun("fixme")]).unwrap(),
            b" fn foo() {}fn main() {}"
        );
        let src = b" fn foo() {}\nfn fixme() {}\nfn main() {}";
        assert_eq!(
            rust_delete(src, [fun("fixme")]).unwrap(),
            b" fn foo() {}\nfn main() {}"
        );
        let src = b" fn foo() {}\n\nfn fixme() {}\nfn main() {}";
        assert_eq!(
            rust_delete(src, [fun("fixme")]).unwrap(),
            b" fn foo() {}\n\nfn main() {}"
        );
        let src = b" fn foo() {}\nfn fixme() {}\n\nfn main() {}";
        assert_eq!(
            rust_delete(src, [fun("fixme")]).unwrap(),
            b" fn foo() {}\n\nfn main() {}"
        );
        let src = b" fn foo() {}\n\nfn fixme() {}\n\nfn main() {}";
        assert_eq!(
            rust_delete(src, [fun("fixme")]).unwrap(),
            b" fn foo() {}\n\n\nfn main() {}"
        );

        let src = b"fn foo() {}\n          fn fixme() {}\n   fn main() {}";
        assert_eq!(
            rust_delete(src, [fun("fixme")]).unwrap(),
            b"fn foo() {}\n   fn main() {}"
        );
    }

    fn unused(
        kind: UnusedDiagnosticKind,
        name: &str,
    ) -> (UnusedDiagnosticKind, String, Option<usize>) {
        (kind, name.to_owned(), None)
    }

    #[test]
    fn removes_associated_and_foreign_items_but_not_macro_invocations() {
        use UnusedDiagnosticKind::*;
        let src = b"struct S; impl S { m!(); const C: u8 = 0; type T = u8; fn f() {} }\n\
                    extern \"C\" { m!(); fn g(); static X: u8; type U; }\n\
                    m!();";
        assert_eq!(
            rust_delete(
                src,
                [
                    unused(Constant, "C"),
                    unused(TypeAlias, "T"),
                    unused(AssociatedFunction, "f"),
                    unused(Function, "g"),
                    unused(Static, "X"),
                    unused(TypeAlias, "U"),
                    unused(MacroDefinition, "m"),
                ],
            )
            .unwrap(),
            b"struct S; impl S { m!(); }\nextern \"C\" { m!(); }\nm!();"
        );
    }

    #[test]
    fn removes_items_of_every_kind_wherever_they_are_nested() {
        use UnusedDiagnosticKind::*;
        let src = b"mod a { fn f() { enum E {} } union U { x: u8 } static S: u8 = 0; \
                    macro_rules! m { () => {} } struct T; type A = u8; }";
        assert_eq!(
            rust_delete(
                src,
                [
                    unused(Enum, "E"),
                    unused(Union, "U"),
                    unused(Static, "S"),
                    unused(MacroDefinition, "m"),
                    unused(Struct, "T"),
                    unused(TypeAlias, "A"),
                ],
            )
            .unwrap(),
            b"mod a { fn f() { } }"
        );
    }

    #[test]
    fn an_offset_picks_out_one_of_several_items_with_the_same_name() {
        let src = b"mod a { fn f() {} } mod b { fn f() {} }";
        //          0123456789012345678901234567890123
        let second = (UnusedDiagnosticKind::Function, "f".to_owned(), Some(31));
        assert_eq!(
            rust_delete(src, [second]).unwrap(),
            b"mod a { fn f() {} } mod b { }"
        );
        let nowhere = (UnusedDiagnosticKind::Function, "f".to_owned(), Some(30));
        assert_eq!(rust_delete(src, [nowhere]).unwrap(), src);
    }

    #[test]
    fn a_raw_identifier_matches_its_plain_name() {
        let src = b"fn r#match() {}\nfn live() {}";
        assert_eq!(rust_delete(src, [fun("match")]).unwrap(), b"fn live() {}");
    }

    #[test]
    fn rejects_unparsable_sources() {
        assert!(rust_delete(b"fn {", [fun("f")]).is_err());
        assert!(remove_empty_blocks(b"fn {").is_err());
    }

    #[test]
    fn removes_only_inherent_impls_and_extern_blocks_left_empty_and_unattributed() {
        let src = b"struct S;\n\
                    impl S {}\n\
                    #[cfg(test)]\n\
                    impl S {}\n\
                    impl Clone for S { }\n\
                    impl S { fn f() {} }\n\
                    extern \"C\" {}\n\
                    #[link(name = \"c\")]\n\
                    extern \"C\" {}\n\
                    extern \"C\" { fn g(); }\n\
                    mod m {}\n";
        assert_eq!(
            remove_empty_blocks(src).unwrap(),
            b"struct S;\n\
              #[cfg(test)]\n\
              impl S {}\n\
              impl Clone for S { }\n\
              impl S { fn f() {} }\n\
              #[link(name = \"c\")]\n\
              extern \"C\" {}\n\
              extern \"C\" { fn g(); }\n\
              mod m {}\n"
        );
    }

    #[test]
    fn commits_every_change_it_can_and_reports_the_rest() {
        let dir = std::env::temp_dir().join(format!("cauterize-commit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let change = |file_name: PathBuf| Change {
            file_name,
            original_content: b"old".to_vec(),
            proposed_content: b"new".to_vec(),
        };
        let written = dir.join("lib.rs");
        assert!(commit_changes([change(written.clone())]).is_ok());
        assert_eq!(std::fs::read(&written).unwrap(), b"new");

        let missing = dir.join("missing/lib.rs");
        let errors = commit_changes([change(missing.clone()), change(dir.join("m.rs"))]);
        assert_eq!(errors.unwrap_err().len(), 1);
        assert!(!missing.exists());
        assert_eq!(std::fs::read(dir.join("m.rs")).unwrap(), b"new");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
