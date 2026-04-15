#ifndef SPX_UTILSX4_H
#define SPX_UTILSX4_H

#include <stdint.h>

#include "context.h"
#include "fors.h"
#include "params.h"
#include "wotsx1.h"

/**
 * For a given leaf index, computes the authentication path and the resulting
 * root node using Merkle's TreeHash algorithm.
 * Expects the layer and tree parts of the tree_addr to be set, as well as the
 * tree type (i.e. SPX_ADDR_TYPE_HASHTREE or SPX_ADDR_TYPE_FORSTREE).
 * Applies the offset idx_offset to indices before building addresses, so that
 * it is possible to continue counting indices across trees.
 */
#define wots_treehashx1 SPX_NAMESPACE(wots_treehashx1)
void wots_treehashx1(unsigned char *root, unsigned char *auth_path,
		     const spx_ctx* ctx,
		     uint32_t leaf_idx, uint32_t idx_offset, uint32_t tree_height,
		     uint32_t tree_addrx4[8], leaf_info_x1 *info);

#define fors_treehashx1 SPX_NAMESPACE(fors_treehashx1)
void fors_treehashx1(unsigned char *root, unsigned char *auth_path,
		     const spx_ctx* ctx,
		     uint32_t leaf_idx, uint32_t idx_offset, uint32_t tree_height,
		     uint32_t tree_addrx4[8], fors_gen_leaf_info *info);
#endif
