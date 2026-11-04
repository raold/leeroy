// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoSettings.h"

namespace Dojo
{
bool IsInstanceName(std::string_view name)
{
    if (name.empty() || name.size() > kMaxInstanceBytes)
        return false;
    for (char const c : name)
    {
        bool const ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
            || c == '.' || c == '_' || c == ':' || c == '-';
        if (!ok)
            return false;
    }
    return true;
}

bool IsSha256Hex(std::string_view text)
{
    if (text.size() != 64)
        return false;
    for (char const c : text)
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
            return false;
    return true;
}

bool IsAbsolutePath(std::string_view path)
{
    if (!path.empty() && path[0] == '/')
        return true;
    return path.size() >= 3 && ((path[0] >= 'A' && path[0] <= 'Z') || (path[0] >= 'a' && path[0] <= 'z')) && path[1] == ':'
        && (path[2] == '/' || path[2] == '\\');
}

RuntimePlan PlanRuntime(Settings const& settings, bool trainingBuild)
{
    if (!settings.enable)
        return {RuntimeState::Off, "Dojo.Enable = 0"};
    if (!trainingBuild)
        return {RuntimeState::Refused, "Dojo.Enable = 1, but this worldserver was built without AC_ENABLE_DOJO"};
    if (settings.marker != kTrainingMarker)
        return {RuntimeState::Refused, "Dojo.Enable = 1, but Dojo.Marker is not \"" + std::string(kTrainingMarker) + "\""};
    if (!IsInstanceName(settings.instance))
        return {RuntimeState::Refused, "Dojo.Enable = 1, but Dojo.Instance is not 1-64 of A-Z a-z 0-9 . _ : -"};
    bool const named = !settings.scenarioManifest.empty();
    if (named != !settings.scenarioSha256.empty())
        return {RuntimeState::Refused, "Dojo.Scenario.Manifest and Dojo.Scenario.Sha256 must be set together"};
    if (named && !IsAbsolutePath(settings.scenarioManifest))
        return {RuntimeState::Refused, "Dojo.Scenario.Manifest must be an absolute path"};
    if (named && !IsSha256Hex(settings.scenarioSha256))
        return {RuntimeState::Refused, "Dojo.Scenario.Sha256 must be 64 lower-case hex digits"};
    return {RuntimeState::Armed, "training instance " + settings.instance};
}

std::string_view StateName(RuntimeState state)
{
    switch (state)
    {
        case RuntimeState::Off:
            return "off";
        case RuntimeState::Refused:
            return "refused";
        case RuntimeState::Armed:
            return "armed";
    }
    return "unknown";
}

std::string StatusLine(RuntimePlan const& plan, bool trainingBuild)
{
    std::string line = "mod-dojo: ";
    line += StateName(plan.state);
    line += " (" + plan.reason + "), ";
    line += trainingBuild ? "training build" : "disabled build";
    line += plan.state == RuntimeState::Armed ? "; no endpoint, fixtures only through the dojo console commands"
                                              : "; no endpoint, no bot control";
    return line;
}
}
