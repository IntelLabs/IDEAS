#if !defined( FORSX1_H_ )
#define FORSX1_H_ 

#include "context.h"
#include "fors.h"

#define fors_gen_leafx1 SPX_NAMESPACE(fors_gen_leafx1)
void fors_gen_leafx1(unsigned char *leaf,
		     const spx_ctx *ctx,
		     uint32_t addr_idx, fors_gen_leaf_info *info);

#endif /* FORSX1_H_ */
