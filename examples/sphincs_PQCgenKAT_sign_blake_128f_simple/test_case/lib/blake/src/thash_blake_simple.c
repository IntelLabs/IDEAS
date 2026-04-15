#include <stdint.h>
#include <string.h>

#include "../../../app/include/address.h"
#include "../../../app/include/params.h"
#include "../../../app/include/thash.h"
#include "../../../app/include/utils.h"

#include "../include/blake.h"

#if SPX_BLAKE512
static void thash_512(unsigned char *out, const unsigned char *in, unsigned int inblocks,
		      const spx_ctx *ctx, uint32_t addr[8]);
#endif

/**
 * Takes an array of inblocks concatenated arrays of SPX_N bytes.
 */
void thash(unsigned char *out, const unsigned char *in, unsigned int inblocks,
           const spx_ctx *ctx, uint32_t addr[8])
{
#if SPX_BLAKE512
    if (inblocks > 1) {
	thash_512(out, in, inblocks, ctx, addr);
        return;
    }
#endif
    unsigned char outbuf[SPX_BLAKE256_OUTPUT_BYTES];
    SPX_VLA(uint8_t, buf, SPX_N + SPX_ADDR_BYTES + inblocks*SPX_N);

    memcpy(buf, ctx->pub_seed, SPX_N);
    memcpy(buf + SPX_N, addr, SPX_ADDR_BYTES);
    memcpy(buf + SPX_N + SPX_ADDR_BYTES, in, inblocks * SPX_N);

    blake256(outbuf, buf, SPX_N + SPX_ADDR_BYTES + inblocks*SPX_N);
    memcpy(out, outbuf, SPX_N);
}

#if SPX_BLAKE512
static void thash_512(unsigned char *out, const unsigned char *in, unsigned int inblocks,
		      const spx_ctx *ctx, uint32_t addr[8])
{
    unsigned char outbuf[SPX_BLAKE512_OUTPUT_BYTES];
    SPX_VLA(uint8_t, buf, SPX_N + SPX_ADDR_BYTES + inblocks*SPX_N);

    memcpy(buf, ctx->pub_seed, SPX_N);
    memcpy(buf + SPX_N, addr, SPX_ADDR_BYTES);
    memcpy(buf + SPX_N + SPX_ADDR_BYTES, in, inblocks * SPX_N);

    blake512(outbuf, buf, SPX_N + SPX_ADDR_BYTES + inblocks*SPX_N);
    memcpy(out, outbuf, SPX_N);
}
#endif
