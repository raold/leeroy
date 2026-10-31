local AL = LibStub("AceLocale-3.0"):GetLocale("AtlasLoot");
local BabbleBoss = AtlasLoot_GetLocaleLibBabble("LibBabble-Boss-3.0")

-- A small self-written sample in the layout of AtlasLoot's WotLK tables. Item names are made up.

	------------------------
	--- Dungeons & Raids ---
	------------------------

		--------------------
		--- Frostmaw Keep ---
		--------------------

	AtlasLoot_Data["FrostmawKeepWarden"] = {
		{ 1, 0, "INV_Box_01", "=q6=#j1#", ""};
		{ 2, 90001, "", "=q3=Rimed Warden Helm", "=ds=#s1#, #a4#", "", "28%"};
		{ 3, 90002, "", "=q3=Glacier \"Cold\" Wraps", "=ds=#s8#, #a1#", "", "31%"};
		{ 16, 0, "INV_Box_01", "=q6=#j3#", ""};
		{ 17, 47241, "", "=q4=Emblem of Triumph", "=ds=#e15#", "", "100%"};
		{ 18, 90003, "", "=q4=Warden's Last Blade", "=ds=#h1#, #w10#", "", "20%"};
		Next = "FrostmawKeepTrash";
	};

	--[[ a long comment with a fake table: AtlasLoot_Data["Nope"] = { { 1, 1 } }; ]]
	local function helper(x) if x then return x .. "!" end end

		------------------------
		--- Icecrown Citadel ---
		------------------------

		-------------------
		--- Lower Spire ---
		-------------------

	AtlasLoot_Data["ICCFrostLord25Man"] = {
		{ 1, 90010, "", "=q4=Frozen Lordly Choker", "=ds=#s2#"};
		{ 2, 90011, "", "=q4=Spire Stompers", "=ds=#s12#, #a4#"};
		Prev = "ICCOther";
	};

		--------------------------------
		--- Icecrown Citadel HEROIC ---
		--------------------------------

	AtlasLoot_Data["ICCFrostLord25ManHEROIC"] = {
		{ 1, 90020, "", "=q4=Heroic Lordly Choker", "=ds=#s2#"};
	};

	-----------------
	--- Factions ---
	-----------------

		-----------------------
		--- Argent Crusade ---
		-----------------------

	AtlasLoot_Data["ArgentCrusadeSample"] = {
		{ 2, 0, "INV_Shield_36", "=q6=#r4#", ""};
		{ 3, 90030, "", "=q3=Crusader's Tabard of Testing", "=ds=#s7#", "13200 #faction# 770 #arena# / 95 #eoffrost#"};
		Back = "REPMENU";
	};
