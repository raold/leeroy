// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_INVITE_SPOTS_H
#define MOD_LEEROY_INVITE_SPOTS_H

#include <array>
#include <cmath>
#include <cstdint>
#include <string_view>
#include <utility>

namespace Leeroy::Guild
{
// The meeting spots the owner's invitations name ("meet me at the bank", "let's go fishing in Dalaran"): where a
// cast member is put by string_do go_to. Each is where a world NPC of that place stands (world DB creature spawns,
// checked 2026-11-20), so the ground there is known; a spot whose name the Realm's places whitelist also has
// (conf/leeroy_places.csv.dist) carries exactly its coordinates. The mind mirrors this table
// (mind/leeroy_mind/invites/spots.py; its tests read this file). The world thread checks each one against the maps
// before a teleport (bad_spot when its coordinates or zone disagree).
struct InviteSpot
{
    std::string_view name;
    std::uint32_t map = 0;
    std::uint32_t zone = 0;
    float x = 0, y = 0, z = 0, o = 0;
    bool fish = false;   // she can fish from here (playerbots' fishing finds the water nearby)
};

inline constexpr std::array<InviteSpot, 14> kInviteSpots = {{
    // Dalaran: Marcia Chase by the Eventide fountain (coin fishing; the places' dal_fountain), Aludane Whitecloud at
    // Krasus' Landing, Amisi Azuregaze in the Legerdemain Lounge (dal_inn), Uda the Beast in the Filthy Animal,
    // Teller Hanners in the bank (dal_bank).
    {"dal_fountain", 571, 4395, 5707.31f, 612.214f, 646.791f, 1.69297f, true},
    {"dal_krasus", 571, 4395, 5813.37f, 453.403f, 658.834f, 4.45059f, false},
    {"dal_inn", 571, 4395, 5847.97f, 635.435f, 647.574f, 1.0472f, false},
    {"dal_horde_inn", 571, 4395, 5888.69f, 507.898f, 641.653f, 1.8675f, false},
    {"dal_bank", 571, 4395, 5984.58f, 607.113f, 651.264f, 2.79253f, false},
    // Orgrimmar: Innkeeper Gryshka (org_inn), Karus (org_bank).
    {"org_inn", 1, 1637, 1633.99f, -4439.37f, 15.7597f, 2.75762f, false},
    {"org_bank", 1, 1637, 1627.32f, -4376.07f, 12.0576f, 3.4383f, false},
    // Undercity: Innkeeper Norman, Randolph Montague (uc_bank).
    {"uc_inn", 0, 1497, 1635.4f, 223.331f, -43.0198f, 2.84489f, false},
    {"uc_bank", 0, 1497, 1599.96f, 240.65f, -52.0596f, 0.087266f, false},
    // Thunder Bluff: Innkeeper Pala, Torn (tb_bank).
    {"tb_inn", 1, 1638, -1300.28f, 38.4858f, 129.292f, 0.558505f, false},
    {"tb_bank", 1, 1638, -1257.63f, 19.5577f, 128.27f, 1.72788f, false},
    // Silvermoon City: Innkeeper Velandra, Periel.
    {"smc_inn", 530, 3487, 9684.1f, -7363.6f, 12.0145f, 4.4855f, false},
    {"smc_bank", 530, 3487, 9808.54f, -7488.17f, 13.6369f, 3.15905f, false},
    // Booty Bay's docks (the places' booty_bay_docks).
    {"booty_bay_docks", 0, 33, -14297.2f, 530.993f, 8.77916f, 3.98863f, true},
}};

// Two members may meet him at one spot: slot 0 stands 2 yards in front of the spot (the side the NPC faces), slot 1
// 2 yards to the left of that.
inline constexpr std::uint32_t kInviteSpotSlots = 2;
inline constexpr float kInviteSpotFront = 2.0f;
inline constexpr float kInviteSpotSide = 2.0f;

inline InviteSpot const* FindInviteSpot(std::string_view name)
{
    for (auto const& spot : kInviteSpots)
        if (spot.name == name)
            return &spot;
    return nullptr;
}

inline int InviteSpotIndex(std::string_view name)
{
    for (std::size_t i = 0; i < kInviteSpots.size(); ++i)
        if (kInviteSpots[i].name == name)
            return static_cast<int>(i);
    return -1;
}

// Where a member of `slot` stands at `spot` (x, y; the spot's own z and orientation).
inline std::pair<float, float> InviteSpotPoint(InviteSpot const& spot, std::uint32_t slot)
{
    float x = spot.x + std::cos(spot.o) * kInviteSpotFront;
    float y = spot.y + std::sin(spot.o) * kInviteSpotFront;
    if (slot % kInviteSpotSlots == 1)
    {
        x += std::cos(spot.o + 1.5707963f) * kInviteSpotSide;
        y += std::sin(spot.o + 1.5707963f) * kInviteSpotSide;
    }
    return {x, y};
}
}

#endif
