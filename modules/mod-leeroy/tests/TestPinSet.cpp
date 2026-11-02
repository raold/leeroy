// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyPinSet.h"
#include "TestHarness.h"
#include <atomic>
#include <thread>

using namespace Leeroy;
using Guids = std::unordered_set<std::uint32_t>;

LEEROY_TEST(pin_owned_mute_survives_module_disable_until_owner_releases_it)
{
    PinSet pins;
    pins.Set(kPinOwnerBenchmark, {72}, kPinMute);
    CHECK(SpeakerSuppressed(false, false, false, pins.Muted(72)));
    pins.Set(kPinOwnerBenchmark, {});
    CHECK(!SpeakerSuppressed(false, false, false, pins.Muted(72)));
    CHECK(SpeakerSuppressed(true, true, true, false));
    CHECK(!SpeakerSuppressed(false, true, true, false));
    CHECK(!SpeakerSuppressed(true, false, true, false));
    CHECK(!SpeakerSuppressed(true, true, false, false));
}

LEEROY_TEST(pin_owners_replace_without_removing_another_owners_overlap)
{
    PinSet pins;
    pins.Set("realm", {1, 2});
    pins.Set(kPinOwnerBenchmark, {2, 3}, kPinMute);
    CHECK(pins.Union({4}) == Guids({1, 2, 3, 4}));
    pins.Set(kPinOwnerBenchmark, {5}, kPinWatch);
    CHECK(pins.Union({4}) == Guids({1, 2, 4, 5}));
    CHECK(!pins.Muted(2));
    CHECK(pins.Watched(5));
    pins.Set(kPinOwnerBenchmark, {});
    CHECK(pins.Owned(kPinOwnerBenchmark).empty());
    CHECK(pins.Owned("realm") == Guids({1, 2}));
}

LEEROY_TEST(pin_flags_are_union_of_owners_and_empty_owner_is_ignored)
{
    PinSet pins;
    pins.Set("", {9}, kPinMute | kPinWatch);
    pins.Set("realm", {8}, kPinMute);
    pins.Set(kPinOwnerTraining, {8}, kPinWatch);
    CHECK(!pins.Muted(9));
    CHECK(pins.Owned("").empty());
    CHECK(pins.Muted(8) && pins.Watched(8));
    pins.Set("realm", {});
    CHECK(!pins.Muted(8) && pins.Watched(8));
    pins.Set(kPinOwnerTraining, {8}, kPinWatch);
    CHECK(pins.Union({}) == Guids({8}));
}

LEEROY_TEST(pin_exported_wrapper_keeps_reserved_owner_contributions_distinct)
{
    LeeroySetExtraPins(kPinOwnerTraining, {71}, kPinWatch);
    LeeroySetExtraPins(kPinOwnerBenchmark, {72}, kPinMute);
    CHECK(Pins().Owned(kPinOwnerTraining) == Guids({71}));
    CHECK(Pins().Owned(kPinOwnerBenchmark) == Guids({72}));
    CHECK(Pins().Watched(71) && Pins().Muted(72));
    LeeroySetExtraPins(kPinOwnerTraining, {});
    LeeroySetExtraPins(kPinOwnerBenchmark, {});
}

LEEROY_TEST(pin_concurrent_reads_never_observe_a_partial_owner_replacement)
{
    PinSet pins;
    std::atomic<bool> valid{true};
    std::thread writer([&]
    {
        for (unsigned i = 0; i < 20000; ++i)
            pins.Set("stress", i % 2 ? Guids{17, 19} : Guids{}, kPinMute | kPinWatch);
    });
    std::thread reader([&]
    {
        for (unsigned i = 0; i < 20000; ++i)
        {
            auto owned = pins.Owned("stress");
            auto united = pins.Union({31});
            if ((!owned.empty() && owned != Guids({17, 19}))
                || (united != Guids({31}) && united != Guids({17, 19, 31}))) valid = false;
            (void)pins.Muted(17);
            (void)pins.Watched(19);
        }
    });
    writer.join();
    reader.join();
    CHECK(valid.load());
}
