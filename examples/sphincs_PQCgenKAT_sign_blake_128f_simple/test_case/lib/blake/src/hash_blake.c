#include <stdint.h>
#include <string.h>

#include "../../../app/include/address.h"
#include "../../../app/include/hash.h"
#include "../../../app/include/params.h"
#include "../../../app/include/utils.h"

#include "../include/blake.h"

#if SPX_N >= 24
#define SPX_BLAKEX_OUTPUT_BYTES SPX_BLAKE512_OUTPUT_BYTES
#define blakeX blake512
#define blakestateX blakestate512
#define blakeX_init blake512_init
#define blakeX_update blake512_update
#define blakeX_final blake512_final
#define blakeX_mgf1 blake512_mgf1
#else
#define SPX_BLAKEX_OUTPUT_BYTES SPX_BLAKE256_OUTPUT_BYTES
#define blakeX blake256
#define blakestateX blakestate256
#define blakeX_init blake256_init
#define blakeX_update blake256_update
#define blakeX_final blake256_final
#define blakeX_mgf1 blake256_mgf1
#endif

void initialize_hash_function(spx_ctx *ctx)
{
  (void)ctx;
}

/**
 * Computes PRF(key, addr), given a secret key of SPX_N bytes and an address
 */
void prf_addr(unsigned char *out, const spx_ctx *ctx,
	      const uint32_t addr[8])
{
  unsigned char buf[2*SPX_N + SPX_ADDR_BYTES] = {0};
  unsigned char outbuf[SPX_BLAKE256_OUTPUT_BYTES] = {0};

  memcpy(buf, ctx->pub_seed, SPX_N);
  memcpy(buf + SPX_N, addr, SPX_ADDR_BYTES);
  memcpy(buf + SPX_N + SPX_ADDR_BYTES, ctx->sk_seed, SPX_N);

  blake256(outbuf, buf, SPX_N + SPX_ADDR_BYTES);

  memcpy(out, outbuf, SPX_N);
}

/**
 * Computes the message-dependent randomness R, using a secret seed and an
 * optional randomization value as well as the message.
 */
void gen_message_random(unsigned char *R, const unsigned char *sk_prf,
			const unsigned char *optrand,
			const unsigned char *m, unsigned long long mlen,
			const spx_ctx *ctx)
{
  (void)ctx;
  blakestateX S;

  blakeX_init(&S);
  blakeX_update(&S, sk_prf, SPX_N);
  blakeX_update(&S, optrand, SPX_N);
  blakeX_update(&S, m, mlen);
  blakeX_final(&S, R);
}

/**
 * Computes the message hash using R, the public key, and the message.
 * Outputs the message digest and the index of the leaf. The index is split in
 * the tree index and the leaf index, for convenient copying to an address.
 */
void hash_message(unsigned char *digest, uint64_t *tree, uint32_t *leaf_idx,
		  const unsigned char *R, const unsigned char *pk,
		  const unsigned char *m, unsigned long long mlen,
		  const spx_ctx *ctx)
{
  (void)ctx;
#define SPX_TREE_BITS (SPX_TREE_HEIGHT * (SPX_D - 1))
#define SPX_TREE_BYTES ((SPX_TREE_BITS + 7) / 8)
#define SPX_LEAF_BITS SPX_TREE_HEIGHT
#define SPX_LEAF_BYTES ((SPX_LEAF_BITS + 7) / 8)
#define SPX_DGST_BYTES (SPX_FORS_MSG_BYTES + SPX_TREE_BYTES + SPX_LEAF_BYTES)

  unsigned char buf[SPX_DGST_BYTES];
  unsigned char *bufp = buf;
  unsigned char seed[2*SPX_N + SPX_BLAKEX_OUTPUT_BYTES];
  
  blakestateX S;
  blakeX_init(&S);
  
  blakeX_update(&S, R, SPX_N);
  blakeX_update(&S, pk, SPX_PK_BYTES);
  blakeX_update(&S, m, mlen);

  blakeX_final(&S, seed+ 2 * SPX_N);

  memcpy(seed, R, SPX_N);
  memcpy(seed + SPX_N, pk, SPX_N);
  
  blakeX_mgf1(bufp, SPX_DGST_BYTES, seed, 2*SPX_N + SPX_BLAKEX_OUTPUT_BYTES);

  memcpy(digest, bufp, SPX_FORS_MSG_BYTES);
  bufp += SPX_FORS_MSG_BYTES;

#if SPX_TREE_BITS > 64
#error For given height and depth, 64 bits cannot represent all subtrees
#endif

  if (SPX_D == 1) {
    *tree = 0;
  } else {
    *tree = bytes_to_ull(bufp, SPX_TREE_BYTES);
    *tree &= (~(uint64_t)0) >> (64 - SPX_TREE_BITS);
  }
  bufp += SPX_TREE_BYTES;

  *leaf_idx = (uint32_t)bytes_to_ull(bufp, SPX_LEAF_BYTES);
  *leaf_idx &= (~(uint32_t)0) >> (32 - SPX_LEAF_BITS);
}
