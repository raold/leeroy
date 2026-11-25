// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_DOJO_HUMAN_H
#define MOD_DOJO_HUMAN_H
#include <string>
class Player;
namespace Dojo
{
// A fixed synthetic player loaded by the core, without attaching playerbot AI.
// The token prevents one scripted gate from adopting another gate's session.
std::string BeginHumanFixture(std::string const& token);
Player* HumanFixture(std::string const& token);
bool EndHumanFixture(std::string const& token);
std::string HumanFixtureError(std::string const& token);
}
void AddSC_dojo_human();
#endif
