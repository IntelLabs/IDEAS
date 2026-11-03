use std::fmt;

use crate::options::OptionsError;

pub type Result<T, E = Error> = std::result::Result<T, E>;

#[derive(Debug)]
pub enum Error {
    Io(std::io::Error),
    Utf8(std::string::FromUtf8Error),
    CommandLine(OptionsError),
    Args(&'static str),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            Error::Io(err) => err.fmt(f),
            Error::Utf8(err) => err.fmt(f),
            Error::CommandLine(err) => err.fmt(f),
            Error::Args(msg) => write!(f, "invalid command line arguments: {msg}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(err: std::io::Error) -> Self {
        Error::Io(err)
    }
}

impl From<std::string::FromUtf8Error> for Error {
    fn from(err: std::string::FromUtf8Error) -> Self {
        Error::Utf8(err)
    }
}

impl From<OptionsError> for Error {
    fn from(err: OptionsError) -> Self {
        Error::CommandLine(err)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn displays_the_wrapped_error() {
        let io = std::io::Error::new(std::io::ErrorKind::NotFound, "gone");
        assert_eq!(Error::from(io).to_string(), "gone");
        let utf8 = String::from_utf8(vec![0xff]).unwrap_err();
        assert_eq!(Error::from(utf8.clone()).to_string(), utf8.to_string());
        let options = OptionsError::UnexpectedFree("x".into());
        assert_eq!(
            Error::from(options).to_string(),
            "unexpected free argument `x`"
        );
        assert_eq!(
            Error::Args("why").to_string(),
            "invalid command line arguments: why"
        );
    }
}
