from pterodactyl_operator.eula import java_minecraft_server


def test_java_minecraft_variants_need_eula_txt() -> None:
    assert java_minecraft_server("NeoForge", {"MC_VERSION": "26.3", "NEOFORGE_VERSION": "latest"})
    assert java_minecraft_server(
        "Forge Enhanced", {"MC_VERSION": "1.20.1", "FORGE_VERSION": "latest"}
    )
    assert java_minecraft_server("Purpur - DDoS", {"MINECRAFT_VERSION": "latest"})
    assert java_minecraft_server("Witherhosting Modpack", {"SERVER_JARFILE": "server.jar"})


def test_bedrock_and_proxies_do_not_use_eula_txt() -> None:
    assert not java_minecraft_server("Waterfall", {"MINECRAFT_VERSION": "latest"})
    assert not java_minecraft_server("Bungeecord", {"BUNGEE_VERSION": "latest"})
    assert not java_minecraft_server("Vanilla Bedrock", {"BEDROCK_VERSION": "latest"})
    assert not java_minecraft_server("Nukkit", {"NUKKIT_VERSION": "latest"})
    assert not java_minecraft_server("Terraria Vanilla", {"TERRARIA_VERSION": "1.4"})
    assert not java_minecraft_server("Factorio", {"SERVER_JARFILE": "factorio"})
