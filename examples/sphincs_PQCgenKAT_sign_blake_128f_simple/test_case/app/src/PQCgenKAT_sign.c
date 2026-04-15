//
//  PQCgenKAT_sign.c
//
//  Created by Bassham, Lawrence E (Fed) on 8/29/17.
//  Copyright © 2017 Bassham, Lawrence E (Fed). All rights reserved.
//

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "../include/api.h"
#include "../include/rng.h"

#define MAX_MARKER_LEN       50
#define BASE_MLEN            33
#define LOOP_COUNT           7

#define KAT_SUCCESS          0
#define KAT_OVERFLOW        -1
#define KAT_CRYPTO_FAILURE  -2

#ifdef BLAKE_TR
#include "../../lib/blake/include/blake.h"
#if SPX_N >= 24
#define blakestateX blakestate512
#define blakeX_init blake512_init
#define blakeX_update blake512_update
#define blakeX_final blake512_final
#define blakeX_output_bytes 64
#else
#define blakestateX blakestate256
#define blakeX_init blake256_init
#define blakeX_update blake256_update
#define blakeX_final blake256_final
#define blakeX_output_bytes 32
#endif

typedef blakestateX kat_tr_ctx;

static inline void kat_tr_init(kat_tr_ctx *ctx) {
  blakeX_init(ctx);

  static const uint8_t tag[] = "KAT-TRANSCRIPT-v1-BLAKE";
  blakeX_update(ctx, tag, sizeof tag - 1);

  const uint8_t sep = 0x00;
  blakeX_update(ctx, &sep, 1);
}

static inline void kat_tr_absorb_label(kat_tr_ctx *ctx, const char *label) {
  const uint8_t *p = (const uint8_t *)label;
  size_t n = 0; while(p[n]) n++;
  blakeX_update(ctx, p, n);

  const uint8_t sep = 0x00;
  blakeX_update(ctx, &sep, 1);
}

static inline void kat_tr_absorb_u64(kat_tr_ctx *ctx, unsigned long long x) {
  uint8_t le[8];
  size_t i;
  for (i = 0; i < 8; i++) {
    le[i] = (uint8_t)((x >> (8 * i)) & 0xFF);
  }

  uint8_t lenle[8];
  unsigned long long L = 8;
  for(i = 0; i < 8; i++) {
    lenle[i] = (uint8_t)((L >> (8 * i)) & 0xFF);
  }

  blakeX_update(ctx, lenle, 8);
  blakeX_update(ctx, le, 8);
}

static inline void kat_tr_absorb_bytes(kat_tr_ctx *ctx, const uint8_t *buf, size_t len) {
  uint8_t lenle[8];
  unsigned long long L = (unsigned long long) len;
  size_t i;
  for(i = 0; i < 8; i++) {
    lenle[i] = (uint8_t)((L >> (8 * i)) & 0xFF);
  }
  blakeX_update(ctx, lenle, 8);
  if(len) {
    blakeX_update(ctx, buf, len);
  }
}

static inline void kat_tr_final(kat_tr_ctx *ctx, uint8_t out32[32]) {
  unsigned char outbuf[blakeX_output_bytes] = {0};
  blakeX_final(ctx, outbuf);
  memcpy(out32, outbuf, 32);
}
#elif HARAKA_TR
#include "../../lib/haraka/include/haraka.h"

typedef struct {
  spx_ctx inner;
  uint8_t s[65];
} kat_tr_ctx;

static inline void kat_tr_init(kat_tr_ctx *ctx) {
  size_t i;
  for(i = 0; i < SPX_N; ++i) {
    ctx->inner.pub_seed[i] = 0;
    ctx->inner.sk_seed[i] = 0;
  }

  tweak_constants(&ctx->inner);
  haraka_S_inc_init(ctx->s);

  static const uint8_t tag[] = "KAT-TRANSCRIPT-v1-HARAKA";
  haraka_S_inc_absorb(ctx->s, tag, sizeof tag - 1, &ctx->inner);

  const uint8_t sep = 0x00;
  haraka_S_inc_absorb(ctx->s, &sep, 1, &ctx->inner);
}

static inline void kat_tr_absorb_label(kat_tr_ctx *ctx, const char *label) {
  const uint8_t *p = (const uint8_t *)label;
  size_t n = 0; while(p[n]) n++;
  haraka_S_inc_absorb(ctx->s, p, n, &ctx->inner);

  const uint8_t sep = 0x00;
  haraka_S_inc_absorb(ctx->s, &sep, 1, &ctx->inner);
}

static inline void kat_tr_absorb_u64(kat_tr_ctx *ctx, unsigned long long x) {
  uint8_t le[8];
  size_t i;
  for (i = 0; i < 8; i++) {
    le[i] = (uint8_t)((x >> (8 * i)) & 0xFF);
  }

  uint8_t lenle[8];
  unsigned long long L = 8;
  for(i = 0; i < 8; i++) {
    lenle[i] = (uint8_t)((L >> (8 * i)) & 0xFF);
  }

  haraka_S_inc_absorb(ctx->s, lenle, 8, &ctx->inner);
  haraka_S_inc_absorb(ctx->s, le, 8, &ctx->inner);
}

static inline void kat_tr_absorb_bytes(kat_tr_ctx *ctx, const uint8_t *buf, size_t len) {
  uint8_t lenle[8];
  unsigned long long L = (unsigned long long) len;
  size_t i;
  for(i = 0; i < 8; i++) {
    lenle[i] = (uint8_t)((L >> (8 * i)) & 0xFF);
  }
  haraka_S_inc_absorb(ctx->s, lenle, 8, &ctx->inner);
  if(len) {
    haraka_S_inc_absorb(ctx->s, buf, len, &ctx->inner);
  }
}

static inline void kat_tr_final(kat_tr_ctx *ctx, uint8_t out32[32]) {
  haraka_S_inc_finalize(ctx->s);
  haraka_S_inc_squeeze(out32, 32, ctx->s, &ctx->inner);
}
#elif SHA2_TR
#include "../../lib/sha2/include/sha2.h"
#if SPX_N >= 24
#define shaX_inc_init sha512_inc_init
#define shaX_inc_blocks sha512_inc_blocks
#define shaX_inc_finalize sha512_inc_finalize
#define shaX_state_len 72
#define shaX_block_bytes 128
#define shaX_output_bytes 64
#else
#define shaX_inc_init sha256_inc_init
#define shaX_inc_blocks sha256_inc_blocks
#define shaX_inc_finalize sha256_inc_finalize
#define shaX_state_len 40
#define shaX_block_bytes 64
#define shaX_output_bytes 32
#endif

typedef struct {
  uint8_t s[shaX_state_len];
} kat_tr_ctx;

static inline void kat_tr_init(kat_tr_ctx *ctx) {
  static const uint8_t tag[] = "KAT-TRANSCRIPT-v1-SHA2";
  uint8_t block[shaX_block_bytes];
  size_t i;

  for (i = 0; i < sizeof tag - 1; ++i) {
      block[i] = tag[i];
  }
  for (i = sizeof tag - 1; i < shaX_block_bytes; ++i) {
      block[i] = 0;
  }

  shaX_inc_init(ctx->s);
  shaX_inc_blocks(ctx->s, block, 1);
}

static inline void kat_tr_absorb_label(kat_tr_ctx *ctx, const char *label) {
  const uint8_t *p = (const uint8_t *)label;
  size_t n = 0; while(p[n]) n++;
  size_t block_count = (n + 1 + (shaX_block_bytes - 1)) / shaX_block_bytes;

  size_t i;
  for(i = 0; i < block_count; ++i) {
    uint8_t block[shaX_block_bytes];
    size_t j;

    for(j = 0; i * shaX_block_bytes + j < n && j < shaX_block_bytes; ++j) {
      block[j] = p[i * shaX_block_bytes + j];
    }

    if(i * shaX_block_bytes + j == n && j < shaX_block_bytes) {
      block[j] = 0x00;
      ++j;
    }

    for(; j < shaX_block_bytes; ++j) {
      block[j] = 0;
    }

    shaX_inc_blocks(ctx->s, block, 1);
  }
}

static inline void kat_tr_absorb_u64(kat_tr_ctx *ctx, unsigned long long x) {
  uint8_t block[shaX_block_bytes];
  uint8_t le[8];
  size_t i;
  for (i = 0; i < 8; i++) {
    le[i] = (uint8_t)((x >> (8 * i)) & 0xFF);
  }

  uint8_t lenle[8];
  unsigned long long L = 8;
  for(i = 0; i < 8; i++) {
    lenle[i] = (uint8_t)((L >> (8 * i)) & 0xFF);
  }

  for (i = 0; i < 8; ++i) {
      block[i] = lenle[i];
  }
  for (i = 0; i < 8; ++i) {
      block[8+i] = le[i];
  }
  for (i = 16; i < shaX_block_bytes; ++i) {
      block[i] = 0;
  }

  shaX_inc_blocks(ctx->s, block, 1);
}

static inline void kat_tr_absorb_bytes(kat_tr_ctx *ctx, const uint8_t *buf, size_t len) {
  uint8_t lenle[shaX_block_bytes] = {0};
  unsigned long long L = (unsigned long long) len;
  size_t i;
  for(i = 0; i < 8; i++) {
    lenle[i] = (uint8_t)((L >> (8 * i)) & 0xFF);
  }
  size_t block_count = (len + (shaX_block_bytes - 1)) / shaX_block_bytes;
  shaX_inc_blocks(ctx->s, lenle, 1);

  if(len != 0) {
    for(i = 0; i < block_count; ++i) {
      uint8_t block[shaX_block_bytes];
      size_t j;

      for(j = 0; i * shaX_block_bytes + j < len && j < shaX_block_bytes; ++j) {
        block[j] = buf[i * shaX_block_bytes + j];
      }
      for(; j < shaX_block_bytes; ++j) {
        block[j] = 0;
      }

      shaX_inc_blocks(ctx->s, block, 1);
    }
  }
}

static inline void kat_tr_final(kat_tr_ctx *ctx, uint8_t out32[32]) {
  unsigned char outbuf[shaX_output_bytes] = {0};
  uint8_t final_block[shaX_block_bytes] = {0};
  shaX_inc_finalize(outbuf, ctx->s, final_block, 1);
  memcpy(out32, outbuf, 32);
}
#elif SHAKE_TR
#include "../../lib/shake/include/fips202.h"
typedef struct {
  uint64_t s[26];
} kat_tr_ctx;

static inline void kat_tr_init(kat_tr_ctx *ctx) {
  shake256_inc_init(ctx->s);

  static const uint8_t tag[] = "KAT-TRANSCRIPT-v1-SHAKE";
  shake256_inc_absorb(ctx->s, tag, sizeof tag - 1);

  const uint8_t sep = 0x00;
  shake256_inc_absorb(ctx->s, &sep, 1);
}

static inline void kat_tr_absorb_label(kat_tr_ctx *ctx, const char *label) {
  const uint8_t *p = (const uint8_t *)label;
  size_t n = 0; while(p[n]) n++;
  shake256_inc_absorb(ctx->s, p, n);

  const uint8_t sep = 0x00;
  shake256_inc_absorb(ctx->s, &sep, 1);
}

static inline void kat_tr_absorb_u64(kat_tr_ctx *ctx, unsigned long long x) {
  uint8_t le[8];
  size_t i;
  for (i = 0; i < 8; i++) {
    le[i] = (uint8_t)((x >> (8 * i)) & 0xFF);
  }

  uint8_t lenle[8];
  unsigned long long L = 8;
  for(i = 0; i < 8; i++) {
    lenle[i] = (uint8_t)((L >> (8 * i)) & 0xFF);
  }

  shake256_inc_absorb(ctx->s, lenle, 8);
  shake256_inc_absorb(ctx->s, le, 8);
}

static inline void kat_tr_absorb_bytes(kat_tr_ctx *ctx, const uint8_t *buf, size_t len) {
  uint8_t lenle[8];
  unsigned long long L = (unsigned long long) len;
  size_t i;
  for(i = 0; i < 8; i++) {
    lenle[i] = (uint8_t)((L >> (8 * i)) & 0xFF);
  }
  shake256_inc_absorb(ctx->s, lenle, 8);
  if(len) {
    shake256_inc_absorb(ctx->s, buf, len);
  }
}

static inline void kat_tr_final(kat_tr_ctx *ctx, uint8_t out32[32]) {
  shake256_inc_finalize(ctx->s);
  shake256_inc_squeeze(out32, 32, ctx->s);
}
#endif

int
main(void)
{
  static unsigned char  m[BASE_MLEN * LOOP_COUNT];
  static unsigned char  sm[BASE_MLEN * LOOP_COUNT + CRYPTO_BYTES];
  static unsigned char  m1[BASE_MLEN * LOOP_COUNT + CRYPTO_BYTES];
  static unsigned char  pk[CRYPTO_PUBLICKEYBYTES];
  static unsigned char  sk[CRYPTO_SECRETKEYBYTES];
  static unsigned char  seed[48];
  static unsigned char  entropy_input[48];
  static unsigned char  msg[BASE_MLEN * LOOP_COUNT];

  unsigned long long    mlen, smlen, mlen1;
  int                   ret;

  // Deterministic entropy to seed DRBG to make .req
  for (int i = 0; i < 48; i++) {
    entropy_input[i] = (unsigned char)i;
  }
  randombytes_init(entropy_input, NULL);

  // Initialize Transcript
  kat_tr_ctx tctx;
  kat_tr_init(&tctx);
  kat_tr_absorb_label(&tctx, "CRYPTO_ALGNAME");
  kat_tr_absorb_bytes(&tctx, (const uint8_t *)CRYPTO_ALGNAME, strlen(CRYPTO_ALGNAME));
  kat_tr_absorb_label(&tctx, "SKBYTES"); kat_tr_absorb_u64(&tctx, CRYPTO_SECRETKEYBYTES);
  kat_tr_absorb_label(&tctx, "PKBYTES"); kat_tr_absorb_u64(&tctx, CRYPTO_PUBLICKEYBYTES);
  kat_tr_absorb_label(&tctx, "SIGBYTES"); kat_tr_absorb_u64(&tctx, CRYPTO_BYTES);

  for (int i = 0; i < LOOP_COUNT; i++) {
    randombytes(seed, sizeof seed);

    kat_tr_absorb_label(&tctx, "count"); kat_tr_absorb_u64(&tctx, (unsigned long long) i);
    kat_tr_absorb_label(&tctx, "seed"); kat_tr_absorb_bytes(&tctx, seed, sizeof seed);

    mlen = (unsigned long long int)(BASE_MLEN * (i + 1));
    if (mlen > BASE_MLEN * LOOP_COUNT) { fprintf(stderr, "mlen overflow\n"); return KAT_OVERFLOW; }

    kat_tr_absorb_label(&tctx, "mlen"); kat_tr_absorb_u64(&tctx, mlen);

    randombytes(msg, mlen);
    kat_tr_absorb_label(&tctx, "msg"); kat_tr_absorb_bytes(&tctx, msg, mlen);

    memset(m, 0, mlen);
    memset(m1, 0, mlen + CRYPTO_BYTES);
    memset(sm, 0, mlen + CRYPTO_BYTES);
    memcpy(m, msg, mlen);

    // Keypair
    ret = crypto_sign_keypair(pk, sk);
    if (ret) { fprintf(stderr, "crypto_sign_keypair=%d\n", ret); return KAT_CRYPTO_FAILURE; }
    kat_tr_absorb_label(&tctx, "pk"); kat_tr_absorb_bytes(&tctx, pk, CRYPTO_PUBLICKEYBYTES);
    kat_tr_absorb_label(&tctx, "sk"); kat_tr_absorb_bytes(&tctx, sk, CRYPTO_SECRETKEYBYTES);

    // Sign
    ret = crypto_sign(sm, &smlen, m, mlen, sk);
    if (ret) { fprintf(stderr, "crypto_sign=%d\n", ret); return KAT_CRYPTO_FAILURE; }
    kat_tr_absorb_label(&tctx, "smlen"); kat_tr_absorb_u64(&tctx, smlen);
    kat_tr_absorb_label(&tctx, "sm"); kat_tr_absorb_bytes(&tctx, sm, smlen);

    // Verify
    ret = crypto_sign_open(m1, &mlen1, sm, smlen, pk);
    if (ret) { fprintf(stderr, "crypto_sign_open=%d\n", ret); return KAT_CRYPTO_FAILURE; }
    if (mlen1 != mlen) { fprintf(stderr, "mlen mismatch\n"); return KAT_CRYPTO_FAILURE; }
    if (memcmp(m, m1, mlen) != 0) { fprintf(stderr, "m mismatch\n"); return KAT_CRYPTO_FAILURE; }
  }

  // Finalize transcript digest
  uint8_t digest[32] = {0};
  kat_tr_final(&tctx, digest);

  printf("KAT transcript digest = ");
  for (size_t i = 0; i < 32; i++) { printf("%02X", digest[i]); }
  printf("\n");

  return KAT_SUCCESS;
}
