// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
// Windows headers define near as empty. Worldserver includes them before this header;
// reproduce that order on every platform so a new conflicting field fails compilation.
#define near
#include "LeeroyStrings.h"
#undef near
