// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyBotCommandPolicy.h"
#include "TestHarness.h"

#include <cstdint>
#include <limits>

using namespace Leeroy::Guild;

LEEROY_TEST(bot_commands_only_untargeted_group_buffs_default_to_self)
{
    CHECK(BotSpellDefaultsToSelf("bloodlust", 0));
    CHECK(BotSpellDefaultsToSelf("heroism", 0));
    CHECK(!BotSpellDefaultsToSelf("rebirth", 0));
    CHECK(!BotSpellDefaultsToSelf("bloodlust", 101));
    CHECK(!BotSpellDefaultsToSelf("heroism", 101));
    CHECK(!BotSpellDefaultsToSelf("rebirth", 101));
    CHECK(!BotSpellDefaultsToSelf("unknown", 0));
}

LEEROY_TEST(bot_commands_stock_topup_never_deletes_or_underflows)
{
    CHECK_EQ(StockMissing(0, 5), 5u);
    CHECK_EQ(StockMissing(3, 5), 2u);
    CHECK_EQ(StockMissing(5, 5), 0u);
    CHECK_EQ(StockMissing(15, 5), 0u);
    CHECK_EQ(StockMissing(std::numeric_limits<std::uint32_t>::max(), 20), 0u);
}

LEEROY_TEST(bot_commands_full_and_partial_inventory_plans_match_actual_destination_counts)
{
    CHECK(StockPlanCount(5, 0, 5, true, false) == 5u);
    CHECK(StockPlanCount(5, 3, 2, false, true) == 2u);
    CHECK(StockPlanCount(5, 5, 0, false, true) == 0u);
    CHECK(!StockPlanCount(5, 6, 0, false, true));
    CHECK(!StockPlanCount(5, 3, 3, false, true));
    CHECK(!StockPlanCount(5, 3, 1, false, true));
    CHECK(!StockPlanCount(5, 0, 6, true, false));
    CHECK(!StockPlanCount(5, 0, std::uint64_t{1} << 32, true, false));
}

LEEROY_TEST(bot_commands_error_or_inconsistent_inventory_results_cannot_spawn_items)
{
    CHECK(!StockPlanCount(5, 0, 5, false, false));
    CHECK(!StockPlanCount(5, 3, 2, true, false));
    CHECK(!StockPlanCount(5, 0, 5, false, true));
    CHECK(!StockPlanCount(5, 0, 0, true, false));
}

LEEROY_TEST(bot_commands_buff_requires_a_preference_or_one_unambiguous_carried_flask)
{
    CHECK(ChooseBotFlask({}, 0).choice == FlaskChoice::Missing);
    CHECK(ChooseBotFlask({{46377, false, false}}, 0).choice == FlaskChoice::Missing);
    CHECK(ChooseBotFlask({{43015, true, false}, {99999, true, false}}, 0).choice == FlaskChoice::Missing);
    auto one = ChooseBotFlask({{46377, true, false}}, 0);
    CHECK(one.choice == FlaskChoice::Use);
    CHECK_EQ(one.item, 46377u);
    CHECK(ChooseBotFlask({{46377, true, false}, {46379, true, false}}, 0).choice == FlaskChoice::Ambiguous);
    auto preferred = ChooseBotFlask({{46377, true, false}, {46379, true, false}}, 46379);
    CHECK(preferred.choice == FlaskChoice::Use);
    CHECK_EQ(preferred.item, 46379u);
}

LEEROY_TEST(bot_commands_missing_preferred_flask_does_not_choose_another_roles_leftovers)
{
    CHECK(ChooseBotFlask({{46377, false, false}, {46379, true, false}}, 46377).choice == FlaskChoice::Missing);
    CHECK(ChooseBotFlask({{46379, true, false}}, 99999).choice == FlaskChoice::Missing);
}

LEEROY_TEST(bot_commands_existing_flask_aura_is_an_idempotent_noop)
{
    auto const one = ChooseBotFlask({{46376, true, true}}, 0);
    CHECK(one.choice == FlaskChoice::AlreadyActive);
    CHECK_EQ(one.item, 46376u);
    auto const preferred = ChooseBotFlask({{46376, true, false}, {46379, true, true}}, 46379);
    CHECK(preferred.choice == FlaskChoice::AlreadyActive);
    CHECK_EQ(preferred.item, 46379u);
    CHECK(ChooseBotFlask({{46376, true, false}, {46379, true, true}}, 0).choice == FlaskChoice::AlreadyActive);
}

LEEROY_TEST(bot_commands_last_consumed_flask_aura_satisfies_retry_without_consuming_leftovers)
{
    auto const preferred = ChooseBotFlask({{46376, false, true}, {46379, true, false}}, 46376);
    CHECK(preferred.choice == FlaskChoice::AlreadyActive);
    CHECK_EQ(preferred.item, 46376u);
    auto const unique = ChooseBotFlask({{46376, false, true}, {46379, true, false}}, 0);
    CHECK(unique.choice == FlaskChoice::AlreadyActive);
    CHECK_EQ(unique.item, 46376u);
    CHECK(ChooseBotFlask({{46376, false, true}}, 46379).choice == FlaskChoice::Missing);
    CHECK(ChooseBotFlask({{46376, false, true}, {46379, false, true}}, 0).choice == FlaskChoice::Ambiguous);
    CHECK(ChooseBotFlask({{43015, false, true}, {99999, false, true}}, 0).choice == FlaskChoice::Missing);
}
