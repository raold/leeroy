local AL = LibStub("AceLocale-3.0"):GetLocale("AtlasLoot");
local BabbleBoss = AtlasLoot_GetLocaleLibBabble("LibBabble-Boss-3.0")
local BabbleZone = AtlasLoot_GetLocaleLibBabble("LibBabble-Zone-3.0")

if AtlasLoot_TableNames == nil then AtlasLoot_TableNames = {} end

	AtlasLoot_TableNames["FrostmawKeepWarden"] = { BabbleBoss["Warden Frostmaw"].." / "..BabbleBoss["Rime"], "AtlasLootWotLK" };
	AtlasLoot_TableNames["ICCFrostLord25Man"] = { BabbleBoss["The Frost Lord"], "AtlasLootWotLK" };
	AtlasLoot_TableNames["ICCFrostLord25ManHEROIC"] = { BabbleBoss["The Frost Lord"].." ("..AL["Heroic"]..")", "AtlasLootWotLK" };
	AtlasLoot_TableNames["ArgentCrusadeSample"] = { BabbleZone["Argent Crusade"], "AtlasLootRepFaction" };
