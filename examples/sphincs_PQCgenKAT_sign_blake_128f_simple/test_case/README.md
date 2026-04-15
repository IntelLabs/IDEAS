SPHINCS+
==========
This repository contains a modified version of the [SPHINCS+ scheme reference](https://github.com/eyalr0/sphincsplusc/) including Jean-Philippe Aumasson's reference implementation of BLAKE.

SPHINCS+ is a plausibly quantum resilient hash-based signature scheme which was selected by NIST for the [FIPS 205](https://csrc.nist.gov/pubs/fips/205/final) SLH-DSA "Stateless Hash-Based Digital Signature Standard" for post-quantum signatures.
The SPHINCS+ construction combines multiple layers: Winternitz one-time signatures (WOTS+), a few-time signature scheme (FORS), Merkle trees, and a hypertree structure to enable scalable signing without state management.

## Build Instructions
Prerequisites: 
- libcrypto

### Parameters
CMake requires three arguments to build the libraries.
- `HASH_BACKEND` Defining which underlying hash function to use `(blake, sha2, shake, haraka)` 
- `THASH` Determining whether to use the robust or simple construction `(robust, simple)`
- `SECPAR` Determining the security parameter and whether to use short or fast signatures `(128f, 128s, 192f, 192s, 256f, 256s)`

The possible values are all listed in `CMakeLists.txt` with the exception of 
`SECPAR`, which are formatted as the integer parameter with an appended character `s` or `f` indicating whether the signatures should prioritize length (short) or speed (fast). The integer parameter can either be 128, 192 or 256. An example `SECPAR` value is `128s`.

### Build Commands
To build for a particular set of parameters in a subdirectory `build`:
```
mdkir build
cmake -B build -DHASH_BACKEND=sha2 -DTHASH=robust -DSECPAR=192f
cmake --build build
```

## Associated Executable
For testing purposes, there is a provided executable whose main function can be found in [PQCgenKAT_sign.c](./app/src/PQCgenKAT_sign.c). 
This executable performs an in-memory test of signing and verification capabilities before producing a shake256 digest of the signature transcripts.

PQCgenKAT_sign.c links against the underlying hash backend with parameters defined by the CMake arguments `HASH_BACKEND`, `THASH`, and `SECPAR` described earlier.

## License
Following the original code from the [SPHINCS+ submission repository](https://github.com/sphincs/sphincsplus), the [SPHINCS+ reference implementation](https://github.com/eyalr0/sphincsplusc/), and Jean-Philippe's BLAKE implementation, all included code is available under the CC0 1.0 Universal Public Domain Dedication, with the exception of rng.c and rng.h which were provided by NIST, and PQCgenKAT_sign.c which was originally provided by NIST, but we have altered to no longer perform file IO.
