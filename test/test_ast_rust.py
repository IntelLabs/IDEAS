#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from ideas.ast_rust import CodeRust, strip_fns


_NESTED = CodeRust("""\
pub trait Convert {
    type Rust;
    unsafe fn to_rust(cs: *const Self) -> Self::Rust;
}

impl Convert for house_t {
    type Rust = libdriver_rs::HouseT;

    unsafe fn to_rust(cs: *const Self) -> Self::Rust {
        todo!()
    }
}

mod helpers {
    pub fn helper(x: i32) -> i32 {
        x
    }

    pub struct Keep {
        pub a: i32,
    }
}

pub fn top(x: i32) -> i32 {
    x
}
""")


def test_strip_fns_nested_bodies():
    out = str(strip_fns(_NESTED))

    # Bodies go, signatures stay, at every level
    assert "todo!()" not in out
    assert "unsafe fn to_rust(cs: *const Self) -> Self::Rust;" in out
    assert "pub fn helper(x: i32) -> i32;" in out
    assert "pub fn top(x: i32) -> i32;" in out

    # Enclosing headers survive so the signatures stay attributable
    assert "impl Convert for house_t {" in out
    assert "mod helpers {" in out
    assert "type Rust = libdriver_rs::HouseT;" in out


def test_strip_fns_delete_nested():
    out = str(strip_fns(_NESTED, delete=True))

    # Functions disappear wherever they live
    assert "fn to_rust" not in out
    assert "fn helper" not in out
    assert "fn top" not in out

    # Non-function items are untouched
    assert "impl Convert for house_t {" in out
    assert "type Rust = libdriver_rs::HouseT;" in out
    assert "pub struct Keep" in out


def test_strip_fns_empty_body():
    out = str(strip_fns(CodeRust("impl Marker for house_t {}"), delete=True))
    assert out.strip() == "impl Marker for house_t {}"


_CINTEROP_IMPL = CodeRust("""\
impl CInterop for git_iterator_options {
    type Rust = driver_rs::GitIteratorOptions<'static, 'static>;

    unsafe fn to_rust(cs: *const Self) -> Self::Rust {
        let cs = unsafe { &*cs };

        let start = if cs.start.is_null() {
            None
        } else {
            Some(unsafe { std::ffi::CStr::from_ptr(cs.start) }.to_bytes())
        };

        let end = if cs.end.is_null() {
            None
        } else {
            Some(unsafe { std::ffi::CStr::from_ptr(cs.end) }.to_bytes())
        };

        let strings = if cs.pathlist.count == 0 {
            Vec::new()
        } else {
            assert!(
                !cs.pathlist.strings.is_null(),
                "nonempty git_strarray has a null strings pointer"
            );

            unsafe {
                std::slice::from_raw_parts(cs.pathlist.strings, cs.pathlist.count)
                    .iter()
                    .map(|&string| {
                        assert!(
                            !string.is_null(),
                            "git_strarray contains a null string pointer"
                        );
                        std::ffi::CStr::from_ptr(string).to_bytes().to_vec()
                    })
                    .collect()
            }
        };

        driver_rs::GitIteratorOptions {
            start,
            end,
            pathlist: driver_rs::GitStrarray { strings },
            flags: cs.flags as u32,
            oid_type: cs.oid_type as driver_rs::GitOidT,
        }
    }

    unsafe fn sync_to_c(rs: &Self::Rust, cs: *mut Self) {
        let cs = unsafe { &mut *cs };

        let allocate_c_string =
            |bytes: &[u8]| -> *mut ::std::os::raw::c_char {
                let allocation_size = bytes
                    .len()
                    .checked_add(1)
                    .expect("C string allocation size overflow");
                let allocation =
                    unsafe { ::libc::malloc(allocation_size) }
                        as *mut ::std::os::raw::c_char;

                assert!(!allocation.is_null(), "C string allocation failed");

                unsafe {
                    std::ptr::copy_nonoverlapping(
                        bytes.as_ptr(),
                        allocation.cast::<u8>(),
                        bytes.len(),
                    );
                    *allocation.cast::<u8>().add(bytes.len()) = 0;
                }

                allocation
            };

        let sync_borrowed_string = |
            value: Option<&[u8]>,
            slot: *mut *const ::std::os::raw::c_char,
        | {
            let current = unsafe { *slot };

            match value {
                None => unsafe {
                    /*
                     * These are borrowed `const char *` fields, so their old
                     * allocations must not be freed here.
                     */
                    *slot = std::ptr::null();
                },
                Some(bytes) => {
                    let unchanged = !current.is_null()
                        && unsafe {
                            std::ffi::CStr::from_ptr(current).to_bytes() == bytes
                        };

                    if !unchanged {
                        /*
                         * A borrowed C string has no capacity metadata and may
                         * point to read-only storage. A changed value therefore
                         * requires a new C allocation. The replaced pointer is
                         * deliberately not freed because this structure does
                         * not own start/end.
                         */
                        let replacement = allocate_c_string(bytes);
                        unsafe {
                            *slot = replacement.cast_const();
                        }
                    }
                }
            }
        };

        sync_borrowed_string(
            rs.start,
            std::ptr::addr_of_mut!(cs.start),
        );
        sync_borrowed_string(
            rs.end,
            std::ptr::addr_of_mut!(cs.end),
        );

        let old_strings = cs.pathlist.strings;
        let old_count = cs.pathlist.count;
        let new_count = rs.pathlist.strings.len();

        if new_count == old_count {
            if new_count != 0 {
                assert!(
                    !old_strings.is_null(),
                    "nonempty git_strarray has a null strings pointer"
                );

                for (index, bytes) in rs.pathlist.strings.iter().enumerate() {
                    let slot = unsafe { old_strings.add(index) };
                    let current = unsafe { *slot };

                    let unchanged = !current.is_null()
                        && unsafe {
                            std::ffi::CStr::from_ptr(current).to_bytes()
                                == bytes.as_slice()
                        };

                    if !unchanged {
                        let replacement = allocate_c_string(bytes);

                        if !current.is_null() {
                            unsafe {
                                ::libc::free(current.cast());
                            }
                        }

                        unsafe {
                            *slot = replacement;
                        }
                    }
                }
            }
        } else {
            let replacement_strings = if new_count == 0 {
                std::ptr::null_mut()
            } else {
                let allocation_size = new_count
                    .checked_mul(std::mem::size_of::<
                        *mut ::std::os::raw::c_char,
                    >())
                    .expect("git_strarray allocation size overflow");

                let allocation =
                    unsafe { ::libc::malloc(allocation_size) }
                        as *mut *mut ::std::os::raw::c_char;

                assert!(
                    !allocation.is_null(),
                    "git_strarray allocation failed"
                );

                for (index, bytes) in
                    rs.pathlist.strings.iter().enumerate()
                {
                    unsafe {
                        *allocation.add(index) = allocate_c_string(bytes);
                    }
                }

                allocation
            };

            if !old_strings.is_null() {
                for index in 0..old_count {
                    let old_string = unsafe { *old_strings.add(index) };
                    if !old_string.is_null() {
                        unsafe {
                            ::libc::free(old_string.cast());
                        }
                    }
                }

                unsafe {
                    ::libc::free(old_strings.cast());
                }
            }

            cs.pathlist.strings = replacement_strings;
        }

        cs.pathlist.count = new_count;
        cs.flags = rs.flags as ::std::os::raw::c_uint;
        cs.oid_type = rs.oid_type as git_oid_t;
    }
}
""")


def test_strip_fns_cinterop_impl():
    out = str(strip_fns(_CINTEROP_IMPL))

    assert "impl CInterop for git_iterator_options {" in out
    assert "type Rust = driver_rs::GitIteratorOptions<'static, 'static>;" in out
    assert "unsafe fn to_rust(cs: *const Self) -> Self::Rust;" in out
    assert "unsafe fn sync_to_c(rs: &Self::Rust, cs: *mut Self);" in out
