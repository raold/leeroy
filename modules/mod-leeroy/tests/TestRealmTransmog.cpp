// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmTransmog.h"
#include "TestHarness.h"
#include <functional>

using namespace Leeroy::Realm;
namespace
{
constexpr std::uint32_t Slot = 15, Appearance = 60002;
TransmogAuthority Authority()
{
    TransmogAuthority value{true, true, true, 7, 9, 21, 8, "transmog-1", BodyIdentity{}};
    value.body->player = reinterpret_cast<Player*>(std::uintptr_t{101});
    value.body->ai = reinterpret_cast<PlayerbotAI*>(std::uintptr_t{102});
    value.body->session = reinterpret_cast<WorldSession*>(std::uintptr_t{103});
    value.body->account = 11; value.body->guild = 21; value.body->map = 1;
    value.body->classId = 1; value.body->race = 2; value.body->sex = 1;
    return value;
}
TransmogItem Item(std::uint64_t guid, std::uint8_t slot)
{
    TransmogItem item;
    item.object = static_cast<std::uintptr_t>(guid + 1000); item.guid = guid; item.owner = 81;
    item.entry = 60001; item.bag = 255; item.slot = slot; item.equipped = true;
    item.queuePosition = 0xffffffff; item.refundRecipient = 81; item.paidMoney = 123;
    item.paidExtendedCost = 5; item.text = "item text"; item.fields.resize(64, 7);
    return item;
}
TransmogSnapshot Snapshot()
{
    TransmogSnapshot snapshot;
    snapshot.owner = 81; snapshot.money = 100001;
    snapshot.items[Slot] = Item(91, Slot); snapshot.items[0] = Item(92, 0);
    snapshot.visibleEntries[Slot] = snapshot.items[Slot].entry;
    snapshot.visibleEntries[0] = snapshot.items[0].entry;
    snapshot.visibleEnchants[Slot] = 1234;
    return snapshot;
}
struct Harness
{
    TransmogSnapshot now = Snapshot();
    TransmogAuthority original = Authority(), current = original;
    bool available = true, metadata = true, readable = true, omitMutation = false;
    std::uint32_t slot = Slot, entry = Appearance;
    std::string eligibility;
    unsigned reads = 0, eligibleCalls = 0, metadataCalls = 0, exactCalls = 0, applies = 0, clears = 0;
    std::function<void(Harness&)> validateInterference, mutationInterference;
    TransmogResult Run()
    {
        return MutateTransmog(available, slot, entry,
            [&] { ++eligibleCalls; return eligibility; },
            [&]() -> std::optional<TransmogSnapshot>
            { ++reads; return readable ? std::optional<TransmogSnapshot>{now} : std::nullopt; },
            [&] { ++metadataCalls; if (validateInterference) validateInterference(*this); return metadata; },
            [&] { ++exactCalls; return SameTransmogAuthority(original, current, "transmog-1"); },
            [&]
            {
                ++applies;
                if (!omitMutation)
                {
                    now.appearances[slot] = {true, true, now.owner, entry, entry};
                    now.visibleEntries[slot] = entry;
                }
                if (mutationInterference) mutationInterference(*this);
            },
            [&]
            {
                ++clears;
                if (!omitMutation)
                { now.appearances[slot] = {}; now.visibleEntries[slot] = now.items[slot].entry; }
                if (mutationInterference) mutationInterference(*this);
            });
    }
};
}
LEEROY_TEST(RealmTransmog_ApplyTouchesOnlyExactEquippedAppearance)
{
    Harness h; auto const before = h.now; auto const result = h.Run();
    CHECK(result.state == TransmogState::Complete); CHECK(result.reason.empty());
    CHECK_EQ(h.applies, 1u); CHECK_EQ(h.clears, 0u);
    CHECK(ProveTransmog(before, h.now, Slot, Appearance));
    CHECK(h.now.items == before.items); CHECK_EQ(h.now.money, before.money);
    CHECK_EQ(h.now.visibleEnchants[Slot], before.visibleEnchants[Slot]);
}
LEEROY_TEST(RealmTransmog_ClearUsesDeletionAndAcceptsOnlyActualAbsence)
{
    for (auto const existing : {0u, Appearance})
    {
        Harness h; h.entry = 0;
        // An indexed zero is a real native record and must still be deleted.
        h.now.appearances[Slot] = {true, true, h.now.owner, existing, existing};
        h.now.visibleEntries[Slot] = existing ? existing : h.now.items[Slot].entry;
        auto const result = h.Run(); CHECK(result.state == TransmogState::Complete);
        CHECK_EQ(h.clears, 1u); CHECK_EQ(h.applies, 0u); CHECK_EQ(h.metadataCalls, 0u);
        CHECK(h.now.appearances[Slot] == TransmogAppearance{});
        CHECK_EQ(h.now.visibleEntries[Slot], h.now.items[Slot].entry);
    }
    Harness absent; absent.entry = 0; CHECK(absent.Run().state == TransmogState::Complete);
}
LEEROY_TEST(RealmTransmog_ZeroReadbackCannotHideARecordLeftByClear)
{
    for (unsigned index = 0; index < 3; ++index)
    {
        Harness h; h.entry = 0;
        h.mutationInterference = [index](Harness& changed)
        {
            auto& appearance = changed.now.appearances[Slot];
            appearance.indexed = index != 1; appearance.recorded = index != 0;
            appearance.owner = appearance.indexed ? changed.now.owner : 0;
            appearance.entry = appearance.readback = 0;
        };
        CHECK(h.Run().state == TransmogState::Unknown); CHECK_EQ(h.clears, 1u);
    }
}
LEEROY_TEST(RealmTransmog_UnavailableNeverConsultsOrMutatesNativeState)
{
    Harness h; h.available = false; auto const before = h.now; auto const result = h.Run();
    CHECK(result.state == TransmogState::Refused); CHECK(result.reason == "no_transmog");
    CHECK_EQ(h.reads + h.eligibleCalls + h.metadataCalls + h.exactCalls + h.applies + h.clears, 0u);
    CHECK(h.now == before);
}
LEEROY_TEST(RealmTransmog_MetadataEligibilityMissingItemAndTradeRefuseBeforeMutation)
{
    Harness h; h.metadata = false; auto result = h.Run();
    CHECK(result.state == TransmogState::Refused); CHECK(result.reason == "bad_entry");
    CHECK_EQ(h.applies + h.clears, 0u);
    h = {}; h.eligibility = "busy:training_owner"; result = h.Run();
    CHECK(result.state == TransmogState::Refused); CHECK(result.reason == h.eligibility); CHECK_EQ(h.reads, 0u);
    h = {}; h.now.items[Slot] = {}; result = h.Run();
    CHECK(result.state == TransmogState::Refused); CHECK(result.reason == "not_equipped");
    h = {}; h.now.items[Slot].inTrade = true; result = h.Run();
    CHECK(result.state == TransmogState::Refused); CHECK(result.reason == "busy:trade");
    h = {}; h.slot = 19; result = h.Run();
    CHECK(result.state == TransmogState::Refused); CHECK_EQ(h.applies + h.clears, 0u);
}
LEEROY_TEST(RealmTransmog_ReplacementBeforeMutationCannotBecomeTheTarget)
{
    Harness h;
    h.validateInterference = [](Harness& changed)
    { changed.now.items[Slot] = Item(93, Slot); };
    auto const result = h.Run(); CHECK(result.state == TransmogState::Refused);
    CHECK(result.reason == "transmog_item_changed"); CHECK_EQ(h.applies + h.clears, 0u);
}
LEEROY_TEST(RealmTransmog_PostMutationItemAndEveryUpdateFieldChangeRemainUnknown)
{
    std::vector<std::function<void(TransmogItem&)>> changes{
        [](auto& item) { ++item.object; }, [](auto& item) { ++item.guid; },
        [](auto& item) { ++item.owner; }, [](auto& item) { ++item.entry; },
        [](auto& item) { ++item.slot; }, [](auto& item) { --item.bag; },
        [](auto& item) { item.equipped = false; }, [](auto& item) { item.inTrade = true; },
        [](auto& item) { ++item.container; }, [](auto& item) { ++item.updateState; },
        [](auto& item) { ++item.queuePosition; }, [](auto& item) { ++item.refundRecipient; },
        [](auto& item) { ++item.paidMoney; }, [](auto& item) { ++item.paidExtendedCost; },
        [](auto& item) { item.text += "changed"; }};
    for (auto const& change : changes)
    {
        Harness h; h.mutationInterference = [&](Harness& changed) { change(changed.now.items[Slot]); };
        CHECK(h.Run().state == TransmogState::Unknown); CHECK_EQ(h.applies, 1u);
    }
    for (std::size_t field = 0; field < Snapshot().items[Slot].fields.size(); ++field)
    {
        Harness h;
        h.mutationInterference = [field](Harness& changed) { ++changed.now.items[Slot].fields[field]; };
        CHECK(h.Run().state == TransmogState::Unknown);
    }
    Harness missing; missing.mutationInterference = [](Harness& changed) { changed.now.items[Slot] = {}; };
    CHECK(missing.Run().state == TransmogState::Unknown);
}
LEEROY_TEST(RealmTransmog_UnrelatedEquipmentMoneyEnchantmentAndAppearanceInterferenceStayUnknown)
{
    std::vector<std::function<void(Harness&)>> changes{
        [](auto& h) { ++h.now.money; }, [](auto& h) { ++h.now.visibleEnchants[Slot]; },
        [](auto& h) { ++h.now.visibleEntries[0]; }, [](auto& h) { ++h.now.items[0].fields[0]; },
        [](auto& h) { h.now.items[0] = Item(93, 0); },
        [](auto& h) { h.now.appearances[0] = {true, true, h.now.owner, 42, 42}; },
        [](auto& h) { h.now.appearances[Slot].owner = 99; },
        [](auto& h) { ++h.now.appearances[Slot].entry; },
        [](auto& h) { ++h.now.appearances[Slot].readback; },
        [](auto& h) { ++h.now.visibleEntries[Slot]; }};
    for (auto const& change : changes)
    { Harness h; h.mutationInterference = change; CHECK(h.Run().state == TransmogState::Unknown); }
}
LEEROY_TEST(RealmTransmog_MissingNativeEffectOrReadbackCannotClaimSuccess)
{
    Harness h; h.omitMutation = true; CHECK(h.Run().state == TransmogState::Unknown);
    h = {}; h.mutationInterference = [](Harness& changed) { changed.readable = false; };
    CHECK(h.Run().state == TransmogState::Unknown);
    h = {}; h.readable = false; CHECK(h.Run().state == TransmogState::Refused); CHECK_EQ(h.applies, 0u);
}
LEEROY_TEST(RealmTransmog_OriginalAuthorityCannotBeRebasedAfterNativeMutation)
{
    std::vector<std::function<void(TransmogAuthority&)>> changes{
        [](auto& a) { a.on = false; }, [](auto& a) { a.castMember = false; },
        [](auto& a) { a.mutationAllowed = false; }, [](auto& a) { ++a.epoch; },
        [](auto& a) { ++a.generation; }, [](auto& a) { ++a.guild; }, [](auto& a) { ++a.human; },
        [](auto& a) { a.holder = "foreign"; }, [](auto& a) { a.holder.clear(); },
        [](auto& a) { a.body.reset(); }, [](auto& a) { a.body->player = nullptr; },
        [](auto& a) { a.body->ai = nullptr; }, [](auto& a) { a.body->session = nullptr; },
        [](auto& a) { ++a.body->account; }, [](auto& a) { ++a.body->guild; },
        [](auto& a) { ++a.body->map; }, [](auto& a) { ++a.body->instance; },
        [](auto& a) { ++a.body->group; }, [](auto& a) { ++a.body->classId; },
        [](auto& a) { ++a.body->race; }, [](auto& a) { ++a.body->sex; }};
    for (auto const& change : changes)
    {
        Harness h; h.mutationInterference = [&](Harness& changed) { change(changed.current); };
        auto result = h.Run(); CHECK(result.state == TransmogState::Unknown);
        CHECK(result.reason == "transmog_identity_unproved"); CHECK_EQ(h.applies, 1u);
        h = {}; h.validateInterference = [&](Harness& changed) { change(changed.current); };
        result = h.Run(); CHECK(result.state == TransmogState::Refused); CHECK_EQ(h.applies, 0u);
    }
}
LEEROY_TEST(RealmTransmog_InvalidOriginalIndexesOrDuplicateEquipmentRefuse)
{
    Harness h; h.now.appearances[Slot] = {true, false, h.now.owner, 0, 0};
    CHECK(h.Run().state == TransmogState::Refused); CHECK_EQ(h.applies, 0u);
    h = {}; h.now.items[0].guid = h.now.items[Slot].guid;
    CHECK(h.Run().state == TransmogState::Refused); CHECK_EQ(h.applies, 0u);
    h = {}; h.now.items[Slot].fields.clear(); CHECK(h.Run().state == TransmogState::Refused);
    h = {}; h.current.holder = ""; CHECK(h.Run().state == TransmogState::Refused);
}
