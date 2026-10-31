function AtlasLoot_FixText(text)
    text = gsub(text, "#c10#", LOCALIZED_CLASS_NAMES_MALE["DEATHKNIGHT"]);
    text = gsub(text, "#p2#", (GetSpellInfo(2018)));	-- Blacksmithing
    text = gsub(text, "#a1#", BabbleInventory["Cloth"]);
    text = gsub(text, "#a4#", BabbleInventory["Plate"]);
    text = gsub(text, "#s1#", BabbleInventory["Head"]);
    text = gsub(text, "#s2#", BabbleInventory["Neck"]);
    text = gsub(text, "#s7#", BabbleInventory["Tabard"]);
    text = gsub(text, "#s8#", BabbleInventory["Wrist"]);
    text = gsub(text, "#s12#", BabbleInventory["Feet"]);
    text = gsub(text, "#h1#", BabbleInventory["One-Hand"]);
    text = gsub(text, "#w10#", BabbleInventory["Sword"]);
    text = gsub(text, "#e15#", AL["Token"]);
    text = gsub(text, "#j1#", AL["Normal Mode"]);
    text = gsub(text, "#j3#", AL["Heroic Mode"]);
    text = gsub(text, "#r4#", BabbleFaction["Revered"]);
    return text;
end
