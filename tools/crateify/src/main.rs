use std::env;
use std::fs;
use std::io;
use std::io::{Error, ErrorKind, Write};
use std::path::Path;

// inspired by https://github.com/stepancheg/rust-protobuf/blob/7131fb244fb1246d2835f5ad7426e607ee7c4a1f/protobuf-codegen/src/gen/mod_rs.rs
fn gen_interm_mod_rs(path: &Path, mods: Vec<String>) -> io::Result<()> {
    // skip if we have no mods
    if mods.is_empty() {
        return Ok(());
    }

    let mod_path = path.join("mod.rs");
    let mut f = fs::File::create(mod_path)?;

    let mut sorted: Vec<String> = mods.into_iter().collect();
    sorted.sort();
    for m in sorted {
        f.write_fmt(format_args!("pub mod {m};\n"))?;
    }

    Ok(())
}

/// Recurses through the pre-generated Rust translation directory an generates the required mod.rs files at each directory layer
fn crateify(input_path: &Path) -> io::Result<()> {
    if input_path.is_dir() {
        let mut mods = Vec::<String>::new();

        for entry in fs::read_dir(&input_path)? {
            let entry = entry?;
            let path = entry.path();
            if path.is_dir() {
                // save the sub mod name so we can include it in the mod.rs
                let submod_dir_str = path.to_str().unwrap();
                let mod_name = Path::new(&submod_dir_str).file_name().unwrap();

                if let Some(m) = mod_name.to_str() {
                    mods.push(m.to_string());
                }

                crateify(&path)?;
            } else {
                // we've reached the deepest directory, so we treat each .rs
                // source file as its own module
                let ext = path.extension().unwrap();
                if let Some(e) = ext.to_str() {
                    if e == "rs" {
                        let mod_name = path.file_stem().unwrap();
                        if let Some(m) = mod_name.to_str() {
                            mods.push(m.to_string());
                        }
                    }
                }
            }
        }
        gen_interm_mod_rs(&input_path, mods)?;
    }
    Ok(())
}

fn main() -> io::Result<()> {
    let args: Vec<String> = env::args().collect();

    // throw an error if we dont' receive any args
    if args.len() < 2 {
        return Err(Error::new(
            ErrorKind::InvalidInput,
            "crateify expects one input argument for the crate directory",
        ));
    }

    // ignore any other args besides the top-level translation dir
    let translation_dir = &args[1];

    crateify(Path::new(translation_dir))
}
