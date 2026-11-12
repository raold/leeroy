// This file is part of leeroy (mod-leeroy), released under GNU GPL v2. See LICENSE.
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h> // Includes Windows' near/far macros before the protocol header.
#endif
#include "LeeroySocialSit.h"
#include "LeeroyRealmCommands.h"
#include "TestHarness.h"
using namespace Leeroy::Realm;

LEEROY_TEST(SocialSit_exact_token_and_bounded_duration)
{
    SocialSeats seats;
    CHECK(!seats.Begin(101,"a",100,901,21,1));
    CHECK(!seats.Begin(101,"a",100,0,21,1));
    CHECK(seats.Begin(101,"a",100,30,21,1));
    CHECK(!seats.Begin(101,"b",100,30,21,1));
    CHECK(!seats.End(101,"b"));
    CHECK(seats.End(101,"a"));
    CHECK(!seats.End(101,"a"));
}

LEEROY_TEST(SocialSit_expiry_context_and_body_release)
{
    SocialSeats seats;
    CHECK(seats.Begin(101,"a",100,30,21,1));
    auto const seat = seats.Entries().at(101);
    CHECK(seats.Current(seat,30099,true,21,1));
    CHECK(!seats.Current(seat,30100,true,21,1));
    CHECK(!seats.Current(seat,100,false,21,1));
    CHECK(!seats.Current(seat,100,true,22,1));
    CHECK(!seats.Current(seat,100,true,21,2));
}

LEEROY_TEST(SocialSit_optional_wire_fields_and_windows_visit_signature)
{
    std::string error;
    nlohmann::json j = {{"guid",101u},{"emote","sit"},{"social",true},{"lease_seconds",30u}};
    auto sit = ParseEmote(j,error);
    CHECK(sit && sit->leaseSeconds == 30);
    j["emote"] = "stand";
    CHECK(!ParseEmote(j,error));
    j.erase("lease_seconds"); j["seat_id"] = "a";
    CHECK(ParseEmote(j,error).has_value());
    j["social"] = false;
    CHECK(!ParseEmote(j,error));
    auto event = MakeVisitState("probe","org_inn",21,1,true,true);
    CHECK(event["near"] == true);
}
