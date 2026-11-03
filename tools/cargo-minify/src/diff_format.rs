use std::str::FromStr;

use crate::cauterize::Change;

const BEFORE_CONTEXT: isize = 3;
const AFTER_CONTEXT: isize = 3;

pub fn println(change: &Change, color_mode: ColorMode) {
    let text = format!("#\n#\tshowing diff for {:?}:\n#", change.file_name());
    if color_mode.enabled() {
        println!("{}", Color::DarkGray.paint(&text));
    } else {
        println!("{text}")
    }

    let left = String::from_utf8_lossy(change.original_content());
    let right = String::from_utf8_lossy(change.proposed_content());

    let diff = diff::lines(&left, &right);

    let mut included = Vec::new();

    let mut last_change: isize = -AFTER_CONTEXT - 1;
    let mut last_insert: isize = -AFTER_CONTEXT - 1;
    for index in 0..diff.len() as isize {
        if has_changed(&diff[index as usize]) {
            if last_insert < index - BEFORE_CONTEXT - 1 {
                included.push(DiffLine::Ellipsis);
            }
            for index in (index - BEFORE_CONTEXT).max(last_insert + 1).max(0)..index {
                included.push(DiffLine::Context(get_line(&diff[index as usize])));
            }
            included.push(DiffLine::Diff(diff[index as usize].clone()));
            last_insert = index;
            last_change = index;
        } else if index - last_change <= AFTER_CONTEXT {
            included.push(DiffLine::Context(get_line(&diff[index as usize])));
            last_insert = index;
        }
    }
    if last_insert < diff.len() as isize - 1 {
        included.push(DiffLine::Ellipsis);
    }

    for line in included {
        let (symbol, color, line) = match line {
            DiffLine::Diff(diff::Result::Left(line)) => ('-', Color::LightRed, line),
            DiffLine::Diff(diff::Result::Right(line)) => ('+', Color::LightGreen, line),
            DiffLine::Diff(diff::Result::Both(_, _)) => unreachable!(),
            DiffLine::Context(line) => (' ', Color::Default, line),
            DiffLine::Ellipsis => ('#', Color::DarkGray, "..."),
        };

        let format = format!("{symbol}\t{line}");

        if color_mode.enabled() {
            println!("{}", color.paint(&format));
        } else {
            println!("{format}");
        }
    }
}

fn has_changed(diff: &diff::Result<&str>) -> bool {
    match diff {
        diff::Result::Left(_) | diff::Result::Right(_) => true,
        diff::Result::Both(_, _) => false,
    }
}

fn get_line<'a>(diff: &diff::Result<&'a str>) -> &'a str {
    match diff {
        diff::Result::Left(line) | diff::Result::Right(line) => line,
        diff::Result::Both(line, _) => line,
    }
}

// The ANSI foreground colors the diff uses, written as `ESC[<code>m<text>ESC[0m`
#[derive(Clone, Copy)]
enum Color {
    Default = 39,
    DarkGray = 90,
    LightRed = 91,
    LightGreen = 92,
}

impl Color {
    fn paint(self, text: &str) -> String {
        format!("\x1B[{}m{text}\x1B[0m", self as u8)
    }
}

enum DiffLine<T> {
    Diff(diff::Result<T>),
    Context(T),
    Ellipsis,
}

#[derive(Copy, Clone, Debug, Default)]
pub enum ColorMode {
    #[default]
    Auto,
    Always,
    Never,
}

impl ColorMode {
    pub fn enabled(&self) -> bool {
        match self {
            // TODO: Improve
            ColorMode::Auto => true,
            ColorMode::Always => true,
            ColorMode::Never => false,
        }
    }
}

impl FromStr for ColorMode {
    type Err = UnsupportedPrintColor;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "auto" => Ok(ColorMode::Auto),
            "always" => Ok(ColorMode::Always),
            "never" => Ok(ColorMode::Never),
            _ => Err(UnsupportedPrintColor),
        }
    }
}

#[derive(Debug)]
pub struct UnsupportedPrintColor;

impl std::fmt::Display for UnsupportedPrintColor {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("unsupported color mode, pick any of: auto, always, never")
    }
}

impl std::error::Error for UnsupportedPrintColor {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paints_with_the_codes_of_nu_ansi_term() {
        assert_eq!(Color::Default.paint("x"), "\x1B[39mx\x1B[0m");
        assert_eq!(Color::DarkGray.paint("x"), "\x1B[90mx\x1B[0m");
        assert_eq!(Color::LightRed.paint("- a"), "\x1B[91m- a\x1B[0m");
        assert_eq!(Color::LightGreen.paint(""), "\x1B[92m\x1B[0m");
    }

    #[test]
    fn color_modes() {
        assert!(matches!("auto".parse(), Ok(ColorMode::Auto)));
        assert!(matches!("always".parse(), Ok(ColorMode::Always)));
        assert!(matches!("never".parse(), Ok(ColorMode::Never)));
        assert!(matches!(ColorMode::default(), ColorMode::Auto));
        let err = "Never".parse::<ColorMode>().unwrap_err().to_string();
        assert_eq!(
            err,
            "unsupported color mode, pick any of: auto, always, never"
        );
    }
}
