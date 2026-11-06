// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmCommands.h"
#include "TestHarness.h"
#include <chrono>
#include <filesystem>
#include <fstream>

using namespace Leeroy::Realm;
namespace fs = std::filesystem;

namespace
{
// A scratch configs/modules tree, removed when the test ends.
struct CsvDirs
{
    fs::path root, modules;
    std::string dir; // as the core builds it: GetConfigPath() + "modules/"
    CsvDirs()
    {
        auto const stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        root = fs::temp_directory_path() / ("leeroy-csv-path-" + std::to_string(stamp));
        modules = root / "configs" / "modules";
        fs::create_directories(modules);
        dir = modules.string() + "/";
    }
    ~CsvDirs()
    {
        std::error_code ignored;
        fs::remove_all(root, ignored);
    }
    static void Write(fs::path const& path, std::string const& text) { std::ofstream(path, std::ios::binary) << text; }
};
std::string const kCsv = "item_id,use,max_count\n43015,bank,1000\n";
}

LEEROY_TEST(Realm_CsvRelativePathResolvesInModuleConfigDir)
{
    CsvDirs dirs;
    CsvDirs::Write(dirs.modules / "leeroy_items.csv", kCsv);
    auto const path = ResolveCsvPath(dirs.dir, "leeroy_items.csv");
    CHECK_EQ(fs::path(path), dirs.modules / "leeroy_items.csv");
    CHECK(ReadCsvFile(path) == std::optional<std::string>(kCsv));

    fs::create_directories(dirs.modules / "realm");
    CsvDirs::Write(dirs.modules / "realm" / "places.csv", kCsv);
    CHECK(ReadCsvFile(ResolveCsvPath(dirs.dir, "realm/places.csv")) == std::optional<std::string>(kCsv));

    // The core's own directory on Windows is relative to the working directory.
    CHECK_EQ(fs::path(ResolveCsvPath("configs/modules/", "leeroy_places.csv")),
        fs::path("configs/modules/leeroy_places.csv"));
}

LEEROY_TEST(Realm_CsvAbsolutePathStaysAsWritten)
{
    CsvDirs dirs;
    auto const elsewhere = dirs.root / "elsewhere.csv";
    CsvDirs::Write(elsewhere, kCsv);
    CHECK(elsewhere.is_absolute());
    for (auto const& written : {elsewhere.string(), elsewhere.generic_string()})
    {
        CHECK_EQ(ResolveCsvPath(dirs.dir, written), written);
        CHECK(ReadCsvFile(ResolveCsvPath(dirs.dir, written)) == std::optional<std::string>(kCsv));
    }
    CHECK_EQ(ResolveCsvPath(dirs.dir, "/srv/realm/leeroy_places.csv"), std::string("/srv/realm/leeroy_places.csv"));
}

LEEROY_TEST(Realm_CsvEmptyValueNamesNoFile)
{
    CsvDirs dirs;
    CHECK_EQ(ResolveCsvPath(dirs.dir, ""), std::string());
    CHECK(!ReadCsvFile(ResolveCsvPath(dirs.dir, "")));
    CHECK(!ReadCsvFile(""));
    CHECK(!ReadCsvFile(dirs.dir)); // the directory itself is no file
}

LEEROY_TEST(Realm_CsvMissingFileReadsNothing)
{
    CsvDirs dirs;
    auto const path = ResolveCsvPath(dirs.dir, "leeroy_places.csv");
    CHECK_EQ(fs::path(path), dirs.modules / "leeroy_places.csv");
    CHECK(!ReadCsvFile(path));
    CHECK(!ReadCsvFile((dirs.root / "absent.csv").string()));
    CHECK(!ReadCsvFile(ResolveCsvPath(dirs.dir, (dirs.root / "absent.csv").string())));
}
