#ifndef SPX_BLAKE_H
#define SPX_BLAKE_H

#include <stdint.h>

#define SPX_BLAKE256_OUTPUT_BYTES 32  /* This does not necessarily equal SPX_N */
#define SPX_BLAKE512_OUTPUT_BYTES 64

#if SPX_BLAKE256_OUTPUT_BYTES < SPX_N
    #error Linking against BLAKE-256 with N larger than 32 bytes is not supported
#endif

typedef struct
{
  unsigned int h[8], s[4], t[2];
  int buflen, nullt;
  unsigned char buf[64];
} blakestate256;

typedef struct
{
  unsigned long long h[8], s[4], t[2];
  int buflen, nullt;
  unsigned char buf[128];
} blakestate512;

/* Implementation of Blake-512 */
int blake512(uint8_t *out, const unsigned char *in, unsigned long long inlen);

void blake512_init(blakestate512 *S);
void blake512_compress(blakestate512 *S, const unsigned char *block);
void blake512_update(blakestate512 *S, const unsigned char *in, unsigned long long inlen);
void blake512_final(blakestate512 *S, unsigned char *out);

/* Implementation of Blake-256 */
int blake256(unsigned char *out, const unsigned char *in, unsigned long long inlen);

void blake256_init(blakestate256 *S);
void blake256_compress(blakestate256 *S, const unsigned char *block);
void blake256_update(blakestate256 *S, const unsigned char *in, unsigned long long inlen);
void blake256_final(blakestate256 *S, unsigned char *out);

#define blake256_mgf1 SPX_NAMESPACE(blake256_mgf1)
void blake256_mgf1(unsigned char *out, unsigned long outlen,
		   const unsigned char *in, unsigned long inlen);

#define blake512_mgf1 SPX_NAMESPACE(blake512_mgf1)
void blake512_mgf1(unsigned char *out, unsigned long outlen,
		   const unsigned char *in, unsigned long inlen);

#endif
