#include <Geode/Geode.hpp>
#include <Geode/modify/AppDelegate.hpp>
#include <Geode/modify/PlayLayer.hpp>

#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cerrno>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <initializer_list>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>

using namespace geode::prelude;

namespace {

constexpr std::size_t kMaximumRequestBytes = 16U * 1024U * 1024U;
constexpr auto kMainThreadTimeout = std::chrono::seconds(30);
constexpr int kSocketTimeoutSeconds = 20;
std::atomic_bool g_suppressedResignActive = false;
std::atomic_bool g_suppressedBackground = false;

class RpcError final : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct PlayEvidence {
    GJGameLevel* level = nullptr;
    int deaths = 0;
    double progress = 0.0;
    double bestProgress = 0.0;
    bool completed = false;
    bool deathPending = false;
};

struct Identity {
    std::string username;
    int accountID = 0;
    int userID = 0;
    bool signedIn = false;
};
struct LevelPayload {
    std::string name;
    std::string description;
    std::string levelString;
    int songID = 0;
    int customSongID = 0;
    int objectCount = 0;
};

std::atomic_bool g_gameLoaded = false;
std::atomic_bool g_bridgeEnabled = false;
PlayEvidence g_playEvidence;
std::atomic_uint64_t g_captureSequence = 0;

std::filesystem::path applicationSupportDirectory() {
    auto const* home = std::getenv("HOME");
    if (!home || !*home) {
        throw RpcError("HOME is unavailable; cannot locate the private bridge directory");
    }
    return std::filesystem::path(home) / "Library" / "Application Support" / "GDAITRANS";
}

std::filesystem::path socketPath() {
    return applicationSupportDirectory() / "bridge.sock";
}

void ensurePrivateDirectory(std::filesystem::path const& path) {
    std::error_code error;
    std::filesystem::create_directories(path, error);
    if (error) {
        throw RpcError(fmt::format("cannot create private directory {}: {}", path.string(), error.message()));
    }

    struct stat status {};
    if (::lstat(path.c_str(), &status) != 0) {
        throw RpcError(fmt::format("cannot inspect private directory {}: {}", path.string(), std::strerror(errno)));
    }
    if (!S_ISDIR(status.st_mode) || S_ISLNK(status.st_mode)) {
        throw RpcError(fmt::format("private path is not a real directory: {}", path.string()));
    }
    if (status.st_uid != ::getuid()) {
        throw RpcError(fmt::format("private directory is not owned by the current user: {}", path.string()));
    }
    if (::chmod(path.c_str(), 0700) != 0) {
        throw RpcError(fmt::format("cannot make private directory private: {}", std::strerror(errno)));
    }
}

bool isReady() {
    auto* app = AppDelegate::get();
    auto* game = GameManager::get();
    auto* account = GJAccountManager::get();
    auto* levels = LocalLevelManager::get();
    return g_gameLoaded.load(std::memory_order_acquire) && app && app->m_loadingFinished && game &&
           game->m_loaded && account && levels && levels->m_localLevels;
}

void requireReady() {
    if (!isReady()) {
        throw RpcError("Geometry Dash is still loading; wait until status.ready is true");
    }
}

Identity currentIdentity() {
    auto* account = GJAccountManager::get();
    auto* game = GameManager::get();
    if (!account || !game) {
        throw RpcError("Geometry Dash account state is unavailable");
    }

    Identity identity;
    identity.username = account->m_username;
    identity.accountID = account->m_accountID;
    identity.userID = static_cast<int>(game->m_playerUserID);
    identity.signedIn = identity.accountID > 0 && identity.userID > 0 && !identity.username.empty();
    return identity;
}

Identity requireSignedInIdentity() {
    auto identity = currentIdentity();
    if (!identity.signedIn) {
        throw RpcError("a signed-in Geometry Dash account with a valid account ID and user ID is required");
    }
    return identity;
}

cocos2d::CCArray* localLevels() {
    auto* manager = LocalLevelManager::get();
    if (!manager || !manager->m_localLevels) {
        throw RpcError("local levels are not loaded");
    }
    return manager->m_localLevels;
}

std::size_t localLevelCount() {
    return localLevels()->count();
}

std::pair<GJGameLevel*, std::size_t> findUniqueLocalLevel(std::string const& name) {
    GJGameLevel* match = nullptr;
    std::size_t count = 0;
    auto* levels = localLevels();
    for (unsigned int index = 0; index < levels->count(); ++index) {
        auto* level = typeinfo_cast<GJGameLevel*>(levels->objectAtIndex(index));
        if (level && std::string_view(level->m_levelName) == name) {
            match = level;
            ++count;
        }
    }
    return {match, count};
}

void resetEvidence(GJGameLevel* level) {
    g_playEvidence.level = level;
    g_playEvidence.deaths = 0;
    g_playEvidence.progress = 0.0;
    g_playEvidence.bestProgress = 0.0;
    g_playEvidence.completed = false;
    g_playEvidence.deathPending = false;
}

void observeProgress(PlayLayer* layer) {
    if (!layer || !layer->m_level) {
        return;
    }
    if (g_playEvidence.level != layer->m_level) {
        resetEvidence(layer->m_level);
    }
    auto progress = static_cast<double>(layer->getCurrentPercent());
    if (std::isfinite(progress)) {
        g_playEvidence.progress = std::clamp(progress, 0.0, 100.0);
        g_playEvidence.bestProgress = std::max(g_playEvidence.bestProgress, g_playEvidence.progress);
    }
    g_playEvidence.completed = g_playEvidence.completed || layer->m_hasCompletedLevel;
    if (g_playEvidence.completed) {
        g_playEvidence.progress = 100.0;
    }
}

std::string sceneName() {
    if (PlayLayer::get()) {
        return "play";
    }
    if (LevelEditorLayer::get()) {
        return "editor";
    }
    auto* game = GameManager::get();
    if (game && (game->m_inMenuLayer || game->m_menuLayer)) {
        return "menu";
    }
    return "other";
}

GJGameLevel* activeSceneLevel() {
    if (auto* play = PlayLayer::get()) {
        return play->m_level;
    }
    if (auto* editor = LevelEditorLayer::get()) {
        return editor->m_level;
    }
    return nullptr;
}

matjson::Value statusResult() {
    matjson::Value result = matjson::Value::object();
    result["connected"] = true;
    auto ready = isReady();
    result["ready"] = ready;
    result["scene"] = ready ? sceneName() : "other";

    if (!ready) {
        result["username"] = "";
        result["account_id"] = 0;
        result["user_id"] = 0;
        result["signed_in"] = false;
        result["local_level_count"] = 0;
        result["playing"] = false;
        result["completed"] = false;
        result["deaths"] = 0;
        result["progress"] = 0.0;
        return result;
    }

    auto identity = currentIdentity();
    auto* play = PlayLayer::get();
    if (play) {
        observeProgress(play);
    }

    result["username"] = identity.username;
    result["account_id"] = identity.accountID;
    result["user_id"] = identity.userID;
    result["signed_in"] = identity.signedIn;
    result["local_level_count"] = static_cast<std::uint64_t>(localLevelCount());
    result["playing"] = play != nullptr;
    result["completed"] = g_playEvidence.completed;
    result["deaths"] = g_playEvidence.deaths;
    result["progress"] = g_playEvidence.progress;
    result["best_progress"] = g_playEvidence.bestProgress;
    if (play && play->m_player1) {
        result["player_x"] = play->m_player1->getPositionX();
        result["player_y"] = play->m_player1->getPositionY();
        result["player_dead"] = play->m_player1->m_isDead;
        result["paused"] = play->m_isPaused;
    }
    if (auto* level = activeSceneLevel()) {
        result["level_name"] = std::string(level->m_levelName);
    }
    if (auto* editor = LevelEditorLayer::get(); editor && editor->m_objects) {
        result["editor_object_count"] = editor->m_objects->count();
    }
    return result;
}

void rejectUnknownKeys(matjson::Value const& value, std::initializer_list<std::string_view> allowed) {
    if (!value.isObject()) {
        throw RpcError("params must be an object");
    }
    for (auto const& entry : value) {
        auto key = entry.getKey();
        if (!key) {
            throw RpcError("params contains an invalid key");
        }
        if (std::find(allowed.begin(), allowed.end(), std::string_view(*key)) == allowed.end()) {
            throw RpcError(fmt::format("unknown parameter: {}", *key));
        }
    }
}

matjson::Value const& requiredValue(matjson::Value const& params, std::string_view key) {
    auto value = params.get(key);
    if (!value) {
        throw RpcError(fmt::format("missing required parameter: {}", key));
    }
    return value.unwrap();
}

std::string requiredString(matjson::Value const& params, std::string_view key) {
    auto const& value = requiredValue(params, key);
    if (!value.isString()) {
        throw RpcError(fmt::format("{} must be a string", key));
    }
    auto string = value.asString().unwrap();
    if (string.find('\0') != std::string::npos) {
        throw RpcError(fmt::format("{} must not contain NUL", key));
    }
    return string;
}

bool requiredBool(matjson::Value const& params, std::string_view key) {
    auto const& value = requiredValue(params, key);
    if (!value.isBool()) {
        throw RpcError(fmt::format("{} must be a boolean", key));
    }
    return value.asBool().unwrap();
}

int requiredNonnegativeInt(matjson::Value const& params, std::string_view key) {
    auto const& value = requiredValue(params, key);
    if (!value.isExactlyInt() && !value.isExactlyUInt()) {
        throw RpcError(fmt::format("{} must be an integer", key));
    }
    if (value.isExactlyInt()) {
        auto number = value.asInt().unwrap();
        if (number < 0 || number > std::numeric_limits<int>::max()) {
            throw RpcError(fmt::format("{} is outside the supported nonnegative integer range", key));
        }
        return static_cast<int>(number);
    }
    auto number = value.asUInt().unwrap();
    if (number > static_cast<std::uintmax_t>(std::numeric_limits<int>::max())) {
        throw RpcError(fmt::format("{} is outside the supported nonnegative integer range", key));
    }
    return static_cast<int>(number);
}

double requiredUnitNumber(matjson::Value const& params, std::string_view key) {
    auto const& value = requiredValue(params, key);
    if (!value.isNumber()) {
        throw RpcError(fmt::format("{} must be a number", key));
    }
    auto number = value.asDouble().unwrap();
    if (!std::isfinite(number) || number < 0.0 || number > 1.0) {
        throw RpcError(fmt::format("{} must be finite and between 0 and 1", key));
    }
    return number;
}

bool hasNonWhitespace(std::string_view text) {
    return std::any_of(text.begin(), text.end(), [](unsigned char character) {
        return character != ' ' && character != '\t' && character != '\r' && character != '\n';
    });
}

int countLevelObjects(std::string const& levelString) {
    if (levelString.empty() || levelString.back() != ';') {
        throw RpcError("level_string must contain a settings segment and end with a semicolon");
    }
    auto firstSeparator = levelString.find(';');
    if (firstSeparator == 0 || firstSeparator == std::string::npos) {
        throw RpcError("level_string has no settings segment");
    }

    int count = 0;
    std::size_t start = firstSeparator + 1;
    while (start < levelString.size()) {
        auto end = levelString.find(';', start);
        if (end == std::string::npos) {
            throw RpcError("level_string has an unterminated object segment");
        }
        if (end == start) {
            throw RpcError("level_string contains an empty object segment");
        }
        if (count == std::numeric_limits<int>::max()) {
            throw RpcError("level_string contains too many objects");
        }
        ++count;
        start = end + 1;
    }
    return count;
}
LevelPayload parseLevelPayload(matjson::Value const& params) {
    rejectUnknownKeys(params, {"name", "description", "song_id", "custom_song_id", "level_string", "object_count"});
    LevelPayload payload {
        .name = requiredString(params, "name"),
        .description = requiredString(params, "description"),
        .levelString = requiredString(params, "level_string"),
        .songID = requiredNonnegativeInt(params, "song_id"),
        .customSongID = requiredNonnegativeInt(params, "custom_song_id"),
        .objectCount = requiredNonnegativeInt(params, "object_count"),
    };
    if (!hasNonWhitespace(payload.name)) {
        throw RpcError("name must not be empty or whitespace");
    }
    if (payload.songID != 0 && payload.customSongID != 0) {
        throw RpcError("song_id and custom_song_id cannot both be nonzero");
    }
    auto actualObjectCount = countLevelObjects(payload.levelString);
    if (actualObjectCount != payload.objectCount) {
        throw RpcError(fmt::format(
            "object_count {} does not match the {} objects in level_string", payload.objectCount, actualObjectCount
        ));
    }
    return payload;
}

void applyMutableLevelPayload(GJGameLevel* level, LevelPayload const& payload) {
    level->m_levelDesc = LevelTools::base64EncodeString(payload.description);
    level->m_levelString = ZipUtils::compressString(payload.levelString, false, 11);
    level->m_audioTrack = payload.songID;
    level->m_songID = payload.customSongID;
    level->setObjectCount(payload.objectCount);
}

matjson::Value levelResult(GJGameLevel* level) {
    matjson::Value result = matjson::Value::object();
    result["name"] = std::string(level->m_levelName);
    result["creator"] = std::string(level->m_creatorName);
    result["account_id"] = static_cast<int>(level->m_accountID);
    result["user_id"] = static_cast<int>(level->m_userID);
    result["object_count"] = static_cast<int>(level->m_objectCount);
    result["local_level_count"] = static_cast<std::uint64_t>(localLevelCount());
    return result;
}

matjson::Value checkpoint(matjson::Value const& params) {
    rejectUnknownKeys(params, {});
    requireReady();
    if (LevelEditorLayer::get()) {
        throw RpcError("checkpoint refused while the editor is open because unsaved editor changes may exist");
    }
    auto* app = AppDelegate::get();
    if (!app) {
        throw RpcError("Geometry Dash save service is unavailable");
    }
    app->trySaveGame(true);

    matjson::Value result = matjson::Value::object();
    result["saved"] = true;
    result["local_level_count"] = static_cast<std::uint64_t>(localLevelCount());
    return result;
}

matjson::Value createLevel(matjson::Value const& params) {
    requireReady();
    if (LevelEditorLayer::get()) {
        throw RpcError("create_level refused while the editor is open because unsaved editor changes may exist");
    }
    auto payload = parseLevelPayload(params);
    if (findUniqueLocalLevel(payload.name).second != 0) {
        throw RpcError(fmt::format("a local level named '{}' already exists; no level was created", payload.name));
    }

    auto identity = requireSignedInIdentity();
    auto* manager = GameLevelManager::get();
    auto* app = AppDelegate::get();
    if (!manager) {
        throw RpcError("Geometry Dash level manager is unavailable");
    }
    if (!app) {
        throw RpcError("Geometry Dash save service is unavailable");
    }

    auto beforeCount = localLevelCount();
    auto* level = manager->createNewLevel();
    if (!level) {
        throw RpcError("Geometry Dash failed to create a new local level");
    }

    level->m_levelName = payload.name;
    applyMutableLevelPayload(level, payload);
    level->m_creatorName = identity.username;
    level->m_userID = identity.userID;
    level->setAccountID(identity.accountID);
    level->m_isEditable = true;
    level->m_localOrSaved = true;

    auto [registeredLevel, sameNameCount] = findUniqueLocalLevel(payload.name);
    if (localLevelCount() != beforeCount + 1 || registeredLevel != level || sameNameCount != 1) {
        manager->deleteLevel(level);
        throw RpcError("Geometry Dash did not uniquely register the new level in local level storage");
    }

    app->trySaveGame(true);
    return levelResult(level);
}

matjson::Value updateLevel(matjson::Value const& params) {
    requireReady();
    if (LevelEditorLayer::get()) {
        throw RpcError("update_level refused while the editor is open because unsaved editor changes may exist");
    }
    if (PlayLayer::get()) {
        throw RpcError("update_level refused while play is active; leave the level normally first");
    }
    auto payload = parseLevelPayload(params);
    auto identity = requireSignedInIdentity();

    auto [level, sameNameCount] = findUniqueLocalLevel(payload.name);
    if (sameNameCount == 0) {
        throw RpcError(fmt::format("no local level named '{}' exists", payload.name));
    }
    if (sameNameCount != 1) {
        throw RpcError(fmt::format(
            "update_level refused because {} local levels are named '{}'", sameNameCount, payload.name
        ));
    }
    if (static_cast<int>(level->m_accountID) != identity.accountID ||
        static_cast<int>(level->m_userID) != identity.userID) {
        throw RpcError("update_level refused because the level does not belong to the current signed-in account");
    }
    auto* app = AppDelegate::get();
    if (!app) {
        throw RpcError("Geometry Dash save service is unavailable");
    }

    applyMutableLevelPayload(level, payload);
    level->levelWasAltered();
    app->trySaveGame(true);
    return levelResult(level);
}

matjson::Value openLevel(matjson::Value const& params) {
    rejectUnknownKeys(params, {"name", "mode"});
    requireReady();
    auto name = requiredString(params, "name");
    auto mode = requiredString(params, "mode");
    if (mode != "editor" && mode != "play") {
        throw RpcError("mode must be 'editor' or 'play'");
    }
    if (LevelEditorLayer::get()) {
        throw RpcError("open_level refused while the editor is open because unsaved editor changes may exist");
    }
    if (PlayLayer::get()) {
        throw RpcError("leave the current play scene before opening another level");
    }

    auto [level, sameNameCount] = findUniqueLocalLevel(name);
    if (sameNameCount == 0) {
        throw RpcError(fmt::format("no local level named '{}' exists", name));
    }
    if (sameNameCount != 1) {
        throw RpcError(fmt::format(
            "open_level refused because {} local levels are named '{}'", sameNameCount, name
        ));
    }

    cocos2d::CCScene* scene = nullptr;
    resetEvidence(level);
    if (mode == "editor") {
        scene = LevelEditorLayer::scene(level, false);
    }
    else {
        scene = PlayLayer::scene(level, false, false);
    }
    if (!scene) {
        resetEvidence(nullptr);
        throw RpcError(fmt::format("Geometry Dash failed to create the {} scene", mode));
    }

    auto* director = cocos2d::CCDirector::get();
    if (!director) {
        resetEvidence(nullptr);
        throw RpcError("Cocos director is unavailable");
    }
    director->replaceScene(cocos2d::CCTransitionFade::create(0.25f, scene));

    matjson::Value result = matjson::Value::object();
    result["name"] = std::string(level->m_levelName);
    result["mode"] = mode;
    result["scene"] = mode;
    return result;
}

cocos2d::enumKeyCodes keyCode(std::string const& key) {
    if (key == "space") return cocos2d::KEY_Space;
    if (key == "left") return cocos2d::KEY_Left;
    if (key == "right") return cocos2d::KEY_Right;
    if (key == "escape") return cocos2d::KEY_Escape;
    if (key == "enter") return cocos2d::KEY_Enter;
    if (key == "tab") return cocos2d::KEY_Tab;
    if (key == "e") return cocos2d::KEY_E;
    if (key == "c") return cocos2d::KEY_C;
    if (key == "r") return cocos2d::KEY_R;
    if (key == "up") return cocos2d::KEY_Up;
    if (key == "down") return cocos2d::KEY_Down;
    throw RpcError(fmt::format("unsupported input key: {}", key));
}

matjson::Value input(matjson::Value const& params) {
    rejectUnknownKeys(params, {"key", "down"});
    requireReady();
    auto key = requiredString(params, "key");
    auto down = requiredBool(params, "down");
    auto code = keyCode(key);
    auto* dispatcher = cocos2d::CCKeyboardDispatcher::get();
    if (!dispatcher) {
        throw RpcError("Geometry Dash keyboard dispatcher is unavailable");
    }
    dispatcher->dispatchKeyboardMSG(code, down, false, 0.0);
    return matjson::Value::object();
}

cocos2d::CCMenuItem* matchingMenuItem(cocos2d::CCNode* node, cocos2d::CCPoint const& worldPoint) {
    if (!node || !node->isVisible() || !node->isRunning()) {
        return nullptr;
    }

    if (auto* menu = typeinfo_cast<cocos2d::CCMenu*>(node); menu && menu->isEnabled()) {
        auto* children = menu->getChildren();
        if (children) {
            for (unsigned int index = 0; index < children->count(); ++index) {
                auto* item = typeinfo_cast<cocos2d::CCMenuItem*>(children->objectAtIndex(index));
                if (!item || !item->isVisible() || !item->isRunning() || !item->isEnabled()) {
                    continue;
                }
                auto local = item->convertToNodeSpace(worldPoint);
                auto rect = item->rect();
                rect.origin = cocos2d::CCPointZero;
                if (rect.containsPoint(local)) {
                    return item;
                }
            }
        }
    }

    auto* children = node->getChildren();
    if (!children) {
        return nullptr;
    }
    for (unsigned int offset = 0; offset < children->count(); ++offset) {
        auto index = children->count() - offset - 1;
        auto* child = typeinfo_cast<cocos2d::CCNode*>(children->objectAtIndex(index));
        if (auto* item = matchingMenuItem(child, worldPoint)) {
            return item;
        }
    }
    return nullptr;
}

matjson::Value click(matjson::Value const& params) {
    rejectUnknownKeys(params, {"x", "y"});
    requireReady();
    auto x = requiredUnitNumber(params, "x");
    auto y = requiredUnitNumber(params, "y");
    auto* director = cocos2d::CCDirector::get();
    auto* scene = director ? director->getRunningScene() : nullptr;
    if (!director || !scene) {
        throw RpcError("no running Cocos scene is available");
    }
    auto size = director->getWinSize();
    cocos2d::CCPoint point {
        static_cast<float>(x * size.width),
        static_cast<float>((1.0 - y) * size.height),
    };
    auto* item = matchingMenuItem(scene, point);
    if (!item) {
        throw RpcError("no enabled visible Cocos menu item contains that point");
    }
    item->activate();

    matjson::Value result = matjson::Value::object();
    result["activated"] = true;
    return result;
}

std::filesystem::path nextCapturePath() {
    auto directory = applicationSupportDirectory() / "captures";
    ensurePrivateDirectory(directory);
    auto micros = std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::system_clock::now().time_since_epoch()
    ).count();
    auto sequence = g_captureSequence.fetch_add(1, std::memory_order_relaxed);
    return directory / fmt::format("capture-{}-{}-{}.png", ::getpid(), micros, sequence);
}

matjson::Value capture(matjson::Value const& params) {
    rejectUnknownKeys(params, {});
    requireReady();
    auto* director = cocos2d::CCDirector::get();
    auto* scene = director ? director->getRunningScene() : nullptr;
    if (!director || !scene) {
        throw RpcError("no running Cocos scene is available to capture");
    }
    auto size = director->getWinSize();
    auto width = static_cast<int>(std::lround(size.width));
    auto height = static_cast<int>(std::lround(size.height));
    if (width <= 0 || height <= 0) {
        throw RpcError("the running Cocos scene has invalid dimensions");
    }

    auto* texture = cocos2d::CCRenderTexture::create(width, height, cocos2d::kCCTexture2DPixelFormat_RGBA8888);
    if (!texture) {
        throw RpcError("Cocos failed to allocate a render texture");
    }
    texture->beginWithClear(0.f, 0.f, 0.f, 1.f);
    scene->visit();
    texture->end();

    std::unique_ptr<cocos2d::CCImage> image(texture->newCCImage(true));
    if (!image) {
        throw RpcError("Cocos failed to read the rendered scene");
    }
    auto path = nextCapturePath();
    if (!image->saveToFile(path.c_str(), false)) {
        std::error_code ignored;
        std::filesystem::remove(path, ignored);
        throw RpcError("Cocos failed to encode the rendered scene as PNG");
    }

    struct stat status {};
    if (::lstat(path.c_str(), &status) != 0 || !S_ISREG(status.st_mode) || status.st_uid != ::getuid() ||
        status.st_size <= 0) {
        std::error_code ignored;
        std::filesystem::remove(path, ignored);
        throw RpcError("the captured PNG was not written as a private regular file");
    }
    if (::chmod(path.c_str(), 0600) != 0) {
        std::error_code ignored;
        std::filesystem::remove(path, ignored);
        throw RpcError(fmt::format("cannot make captured PNG private: {}", std::strerror(errno)));
    }

    matjson::Value result = matjson::Value::object();
    result["image"] = std::filesystem::absolute(path).string();
    result["scene"] = sceneName();
    if (auto* level = activeSceneLevel()) {
        result["level_name"] = std::string(level->m_levelName);
    }
    result["width"] = image->getWidth();
    result["height"] = image->getHeight();
    return result;
}

matjson::Value leaveLevel(matjson::Value const& params) {
    rejectUnknownKeys(params, {});
    requireReady();
    bool savedEditor = false;
    if (auto* editor = LevelEditorLayer::get()) {
        auto* pause = EditorPauseLayer::create(editor);
        if (!pause) {
            throw RpcError("could not create the normal editor save/exit controller");
        }
        pause->onSaveAndExit(nullptr);
        savedEditor = true;
    }
    else if (auto* play = PlayLayer::get()) {
        play->onQuit();
    }
    auto result = matjson::Value::object();
    result["saved_editor"] = savedEditor;
    result["leaving"] = true;
    return result;
}

matjson::Value handleMethod(std::string const& method, matjson::Value const& params) {
    if (method == "status") {
        rejectUnknownKeys(params, {});
        return statusResult();
    }
    if (method == "checkpoint") return checkpoint(params);
    if (method == "create_level") return createLevel(params);
    if (method == "update_level") return updateLevel(params);
    if (method == "open_level") return openLevel(params);
    if (method == "leave_level") return leaveLevel(params);
    if (method == "input") return input(params);
    if (method == "click") return click(params);
    if (method == "capture") return capture(params);
    throw RpcError(fmt::format("unknown bridge method: {}", method));
}

matjson::Value successResponse(std::string const& id, matjson::Value result) {
    matjson::Value response = matjson::Value::object();
    response["id"] = id;
    response["result"] = std::move(result);
    return response;
}

matjson::Value errorResponse(std::string const& id, std::string const& error) {
    matjson::Value response = matjson::Value::object();
    response["id"] = id;
    response["error"] = error;
    return response;
}

matjson::Value processRequest(matjson::Value const& request) {
    std::string id;
    try {
        if (!request.isObject()) {
            throw RpcError("request must be an object");
        }
        for (auto const& entry : request) {
            auto key = entry.getKey();
            if (!key || (*key != "id" && *key != "method" && *key != "params")) {
                throw RpcError(fmt::format("unknown request field: {}", key.value_or("<invalid>")));
            }
        }
        id = requiredString(request, "id");
        if (id.empty()) {
            throw RpcError("id must not be empty");
        }
        if (id.size() > 256) {
            throw RpcError("id is too long");
        }
        auto method = requiredString(request, "method");
        auto const& params = requiredValue(request, "params");
        if (!params.isObject()) {
            throw RpcError("params must be an object");
        }
        return successResponse(id, handleMethod(method, params));
    }
    catch (RpcError const& error) {
        return errorResponse(id, error.what());
    }
    catch (std::exception const& error) {
        log::error("Local bridge request failed: {}", error.what());
        return errorResponse(id, "internal bridge error");
    }
    catch (...) {
        log::error("Local bridge request failed with a non-standard exception");
        return errorResponse(id, "internal bridge error");
    }
}

struct MainDispatchState {
    explicit MainDispatchState(matjson::Value value) : request(std::move(value)) {}

    matjson::Value request;
    matjson::Value response;
    std::mutex mutex;
    std::condition_variable condition;
    bool done = false;
    bool cancelled = false;
};

void runRequestOnMainThread(std::shared_ptr<MainDispatchState> const& state) {
    {
        std::lock_guard lock(state->mutex);
        if (state->cancelled || !g_bridgeEnabled.load(std::memory_order_acquire)) {
            return;
        }
    }
    auto response = processRequest(state->request);
    {
        std::lock_guard lock(state->mutex);
        state->response = std::move(response);
        state->done = true;
    }
    state->condition.notify_one();
}

matjson::Value dispatchRequest(matjson::Value request) {
    std::string requestID;
    if (request.isObject()) {
        auto id = request.get("id");
        if (id && id.unwrap().isString()) {
            requestID = id.unwrap().asString().unwrap();
        }
    }
    auto state = std::make_shared<MainDispatchState>(std::move(request));
    // On macOS the Cocos engine has its own thread and autorelease/GL context.
    // AppKit's dispatch_get_main_queue() is NOT that thread.
    Loader::get()->queueInMainThread([state] { runRequestOnMainThread(state); });

    auto deadline = std::chrono::steady_clock::now() + kMainThreadTimeout;
    std::unique_lock lock(state->mutex);
    while (!state->done && g_bridgeEnabled.load(std::memory_order_acquire)) {
        state->condition.wait_until(lock, std::min(deadline, std::chrono::steady_clock::now() + std::chrono::milliseconds(100)));
        if (std::chrono::steady_clock::now() >= deadline) {
            break;
        }
    }
    if (!state->done) {
        state->cancelled = true;
        return errorResponse(requestID, "Geometry Dash main thread did not answer before the bridge timeout");
    }
    return state->response;
}

bool sendAll(int descriptor, std::string_view bytes) {
    while (!bytes.empty()) {
        auto sent = ::send(descriptor, bytes.data(), bytes.size(), 0);
        if (sent < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (sent == 0) return false;
        bytes.remove_prefix(static_cast<std::size_t>(sent));
    }
    return true;
}

void configureClientSocket(int descriptor) {
    timeval timeout {kSocketTimeoutSeconds, 0};
    ::setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    ::setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
#ifdef SO_NOSIGPIPE
    int enabled = 1;
    ::setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof(enabled));
#endif
}

std::optional<std::string> readRequestLine(int descriptor, std::string& error) {
    std::string request;
    request.reserve(4096);
    char buffer[8192];
    for (;;) {
        auto received = ::recv(descriptor, buffer, sizeof(buffer), 0);
        if (received < 0) {
            if (errno == EINTR) continue;
            error = errno == EAGAIN || errno == EWOULDBLOCK ? "request read timed out" : "request read failed";
            return std::nullopt;
        }
        if (received == 0) {
            error = "request ended before its newline terminator";
            return std::nullopt;
        }
        request.append(buffer, static_cast<std::size_t>(received));
        auto newline = request.find('\n');
        if (newline != std::string::npos) {
            auto trailing = std::string_view(request).substr(newline + 1);
            if (hasNonWhitespace(trailing)) {
                error = "only one request is allowed per connection";
                return std::nullopt;
            }
            request.resize(newline);
            if (!request.empty() && request.back() == '\r') {
                request.pop_back();
            }
            if (request.size() > kMaximumRequestBytes) {
                error = "request exceeds the 16 MiB limit";
                return std::nullopt;
            }
            return request;
        }
        if (request.size() > kMaximumRequestBytes) {
            error = "request exceeds the 16 MiB limit";
            return std::nullopt;
        }
    }
}

class BridgeServer final {
public:
    BridgeServer() = default;
    BridgeServer(BridgeServer const&) = delete;
    BridgeServer& operator=(BridgeServer const&) = delete;

    ~BridgeServer() {
        stop();
    }

    void start() {
        if (m_thread.joinable()) {
            return;
        }
        auto path = socketPath();
        ensurePrivateDirectory(path.parent_path());
        auto pathString = path.string();
        if (pathString.size() >= sizeof(static_cast<sockaddr_un*>(nullptr)->sun_path)) {
            throw RpcError("bridge socket path is too long for a Unix socket");
        }
        removeStaleSocket(path);

        auto descriptor = ::socket(AF_UNIX, SOCK_STREAM, 0);
        if (descriptor < 0) {
            throw RpcError(fmt::format("cannot create Unix socket: {}", std::strerror(errno)));
        }

        sockaddr_un address {};
        address.sun_family = AF_UNIX;
        if (pathString.size() >= sizeof(address.sun_path)) {
            ::close(descriptor);
            throw RpcError("bridge socket path is too long for a Unix socket");
        }
        std::memcpy(address.sun_path, pathString.c_str(), pathString.size() + 1);
        auto addressSize = static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + pathString.size() + 1);
#if defined(__APPLE__)
        address.sun_len = static_cast<unsigned char>(addressSize);
#endif
        if (::bind(descriptor, reinterpret_cast<sockaddr*>(&address), addressSize) != 0) {
            auto message = std::string(std::strerror(errno));
            ::close(descriptor);
            throw RpcError(fmt::format("cannot bind bridge socket: {}", message));
        }
        if (::chmod(path.c_str(), 0600) != 0) {
            auto message = std::string(std::strerror(errno));
            ::close(descriptor);
            ::unlink(path.c_str());
            throw RpcError(fmt::format("cannot make bridge socket private: {}", message));
        }
        if (::listen(descriptor, 8) != 0) {
            auto message = std::string(std::strerror(errno));
            ::close(descriptor);
            ::unlink(path.c_str());
            throw RpcError(fmt::format("cannot listen on bridge socket: {}", message));
        }

        struct stat status {};
        if (::lstat(path.c_str(), &status) != 0 || !S_ISSOCK(status.st_mode) || status.st_uid != ::getuid()) {
            ::close(descriptor);
            ::unlink(path.c_str());
            throw RpcError("bridge socket failed its ownership and type check");
        }
        m_socketDevice = status.st_dev;
        m_socketInode = status.st_ino;
        m_path = std::move(path);
        m_descriptor.store(descriptor, std::memory_order_release);
        g_bridgeEnabled.store(true, std::memory_order_release);
        m_thread = std::thread([this] { serve(); });
    }

    void stop() {
        g_bridgeEnabled.store(false, std::memory_order_release);
        auto descriptor = m_descriptor.exchange(-1, std::memory_order_acq_rel);
        if (descriptor >= 0) {
            ::shutdown(descriptor, SHUT_RDWR);
            ::close(descriptor);
        }
        auto client = m_activeClient.load(std::memory_order_acquire);
        if (client >= 0) {
            ::shutdown(client, SHUT_RDWR);
        }
        if (m_thread.joinable() && m_thread.get_id() != std::this_thread::get_id()) {
            m_thread.join();
        }
        unlinkOwnedSocket();
    }

private:
    void removeStaleSocket(std::filesystem::path const& path) {
        struct stat status {};
        if (::lstat(path.c_str(), &status) != 0) {
            if (errno == ENOENT) return;
            throw RpcError(fmt::format("cannot inspect existing bridge socket: {}", std::strerror(errno)));
        }
        if (!S_ISSOCK(status.st_mode) || status.st_uid != ::getuid()) {
            throw RpcError("existing bridge path is not a socket owned by the current user");
        }

        auto probe = ::socket(AF_UNIX, SOCK_STREAM, 0);
        if (probe < 0) {
            throw RpcError(fmt::format("cannot probe existing bridge socket: {}", std::strerror(errno)));
        }
        sockaddr_un address {};
        address.sun_family = AF_UNIX;
        auto pathString = path.string();
        if (pathString.size() >= sizeof(address.sun_path)) {
            ::close(probe);
            throw RpcError("bridge socket path is too long for a Unix socket");
        }
        std::memcpy(address.sun_path, pathString.c_str(), pathString.size() + 1);
        auto addressSize = static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + pathString.size() + 1);
#if defined(__APPLE__)
        address.sun_len = static_cast<unsigned char>(addressSize);
#endif
        auto connected = ::connect(probe, reinterpret_cast<sockaddr*>(&address), addressSize) == 0;
        auto connectError = errno;
        ::close(probe);
        if (connected) {
            throw RpcError("another GDCLI bridge is already listening");
        }
        if (connectError != ECONNREFUSED && connectError != ENOENT) {
            throw RpcError(fmt::format("existing bridge socket cannot be safely replaced: {}", std::strerror(connectError)));
        }

        struct stat current {};
        if (::lstat(path.c_str(), &current) != 0 || current.st_dev != status.st_dev || current.st_ino != status.st_ino) {
            throw RpcError("existing bridge socket changed while it was being checked");
        }
        if (::unlink(path.c_str()) != 0) {
            throw RpcError(fmt::format("cannot remove stale bridge socket: {}", std::strerror(errno)));
        }
    }

    void serve() {
        while (g_bridgeEnabled.load(std::memory_order_acquire)) {
            auto listener = m_descriptor.load(std::memory_order_acquire);
            if (listener < 0) break;
            auto client = ::accept(listener, nullptr, nullptr);
            if (client < 0) {
                if (errno == EINTR) continue;
                if (!g_bridgeEnabled.load(std::memory_order_acquire) || errno == EBADF || errno == EINVAL) break;
                log::warn("Local bridge accept failed: {}", std::strerror(errno));
                continue;
            }
            m_activeClient.store(client, std::memory_order_release);
            handleClient(client);
            m_activeClient.store(-1, std::memory_order_release);
            ::close(client);
        }
    }

    void handleClient(int client) {
        configureClientSocket(client);
        uid_t peerUser = std::numeric_limits<uid_t>::max();
        gid_t peerGroup = std::numeric_limits<gid_t>::max();
        if (::getpeereid(client, &peerUser, &peerGroup) != 0 || peerUser != ::getuid()) {
            return;
        }

        std::string readError;
        auto line = readRequestLine(client, readError);
        matjson::Value response;
        if (!line) {
            response = errorResponse("", readError);
        }
        else {
            auto parsed = matjson::Value::parse(*line);
            if (!parsed) {
                response = errorResponse("", fmt::format("invalid JSON request: {}", parsed.unwrapErr()));
            }
            else {
                response = dispatchRequest(parsed.unwrap());
            }
        }
        auto encoded = response.dump(matjson::NO_INDENTATION);
        encoded.push_back('\n');
        sendAll(client, encoded);
    }

    void unlinkOwnedSocket() {
        if (m_path.empty()) return;
        struct stat status {};
        if (::lstat(m_path.c_str(), &status) == 0 && status.st_dev == m_socketDevice && status.st_ino == m_socketInode &&
            S_ISSOCK(status.st_mode) && status.st_uid == ::getuid()) {
            ::unlink(m_path.c_str());
        }
        m_path.clear();
    }

    std::atomic_int m_descriptor {-1};
    std::atomic_int m_activeClient {-1};
    std::thread m_thread;
    std::filesystem::path m_path;
    dev_t m_socketDevice = 0;
    ino_t m_socketInode = 0;
};

std::unique_ptr<BridgeServer> g_server;

} // namespace

class $modify(GDCLIBackgroundAppDelegate, AppDelegate) {
    void applicationWillResignActive() {
        if (g_bridgeEnabled.load(std::memory_order_acquire)) {
            g_suppressedResignActive.store(true, std::memory_order_release);
            return;
        }
        AppDelegate::applicationWillResignActive();
    }

    void applicationDidEnterBackground() {
        if (g_bridgeEnabled.load(std::memory_order_acquire)) {
            g_suppressedBackground.store(true, std::memory_order_release);
            return;
        }
        AppDelegate::applicationDidEnterBackground();
    }

    void applicationWillEnterForeground() {
        if (g_bridgeEnabled.load(std::memory_order_acquire) &&
            g_suppressedBackground.exchange(false, std::memory_order_acq_rel)) {
            return;
        }
        AppDelegate::applicationWillEnterForeground();
    }

    void applicationWillBecomeActive() {
        if (g_bridgeEnabled.load(std::memory_order_acquire) &&
            g_suppressedResignActive.exchange(false, std::memory_order_acq_rel)) {
            return;
        }
        AppDelegate::applicationWillBecomeActive();
    }
};

class $modify(GDCLIPlayLayer, PlayLayer) {
    bool init(GJGameLevel* level, bool useReplay, bool dontCreateObjects) {
        if (!PlayLayer::init(level, useReplay, dontCreateObjects)) {
            return false;
        }
        resetEvidence(level);
        return true;
    }

    void postUpdate(float delta) {
        PlayLayer::postUpdate(delta);
        observeProgress(this);
    }

    void destroyPlayer(PlayerObject* player, GameObject* object) {
        PlayLayer::destroyPlayer(player, object);
        if (player && (player == m_player1 || player == m_player2) && player->m_isDead) {
            observeProgress(this);
            if (!g_playEvidence.deathPending) {
                ++g_playEvidence.deaths;
                g_playEvidence.deathPending = true;
            }
        }
    }

    void resetLevel() {
        PlayLayer::resetLevel();
        g_playEvidence.deathPending = false;
        observeProgress(this);
    }

    void fullReset() {
        PlayLayer::fullReset();
        g_playEvidence.deathPending = false;
        observeProgress(this);
    }

    void levelComplete() {
        PlayLayer::levelComplete();
        observeProgress(this);
        g_playEvidence.completed = true;
        g_playEvidence.progress = 100.0;
    }
};

$on_game(Loaded) {
    g_gameLoaded.store(true, std::memory_order_release);
}

$on_game(Exiting) {
    g_gameLoaded.store(false, std::memory_order_release);
    if (g_server) {
        g_server->stop();
        g_server.reset();
    }
}

$on_mod(Loaded) {
    try {
        g_server = std::make_unique<BridgeServer>();
        g_server->start();
        log::info("GDCLI local bridge is listening on its private Unix socket");
    }
    catch (std::exception const& error) {
        g_bridgeEnabled.store(false, std::memory_order_release);
        g_server.reset();
        log::error("GDCLI local bridge could not start: {}", error.what());
    }
}
