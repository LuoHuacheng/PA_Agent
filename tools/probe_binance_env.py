"""Read-only connectivity probe for the configured Binance execution environment.

Pings the REST gateway of the *active* environment declared by
config/settings.json (binance_usdm_environment) and, when the section holds
credentials, checks exchangeInfo for the configured symbol. Never places or
changes any order. With --listen-key it additionally exercises an ephemeral
user-data listenKey lifecycle (create -> keepalive -> delete), which is also
order-free.

Usage:
    python tools/probe_binance_env.py            # env from settings.json
    python tools/probe_binance_env.py --env live
    python tools/probe_binance_env.py --listen-key
"""

import argparse
import sys
import urllib.request


def main(argv=None):
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import load_settings
    from pa_agent.trading import binance_env

    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=["auto", "testnet", "live"], default="auto",
                        help="environment to probe (auto = settings.json binance_usdm_environment)")
    parser.add_argument("--listen-key", action="store_true",
                        help="also verify an ephemeral listenKey lifecycle")
    parsed = parser.parse_args(args)

    settings = load_settings(SETTINGS_JSON_PATH)
    env = binance_env.resolve_env(settings)
    if parsed.env != "auto":
        env = binance_env.env_for_key(parsed.env)
    cfg = binance_env.active_cfg(settings) if parsed.env == "auto" else None
    if cfg is None:
        from pa_agent.trading.binance_env import active_cfg

        cfg = active_cfg(settings)
    print(f"== Binance 执行环境: {env.label_zh} ({env.key}) ==")
    print(f"REST gateway : {env.rest_base}")
    print(f"WS gateway   : {env.ws_base}")
    print(f"state file   : trade_records/{env.state_file}")

    ping_url = env.rest_base + "/fapi/v1/ping"
    try:
        with urllib.request.urlopen(ping_url, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="replace")[:200]
        print(f"ping         : OK  (HTTP {resp.status}) {body}")
    except Exception as exc:
        print(f"ping         : FAIL {exc}")
        return 1

    if not (cfg.api_key or "").strip() or not (cfg.api_secret or "").strip():
        print("API key/secret: 未配置, 跳过 exchangeInfo/listenKey 检查")
        return 0

    from pa_agent.trading.binance_usdm_testnet import BinanceUSDMTestnetClient

    client = BinanceUSDMTestnetClient(
        cfg.api_key, cfg.api_secret, base_url=env.rest_base
    )
    try:
        info = client.exchange_info(cfg.symbol)
        print(f"exchangeInfo : OK  symbol={cfg.symbol} status={info.get('status')}")
    except Exception as exc:
        print(f"exchangeInfo : FAIL {exc}")
        return 2

    if parsed.listen_key:
        try:
            key = client.create_listen_key()
            client.keepalive_listen_key(key)
            client.close_listen_key(key)
            print("listenKey    : OK (created/keepalive/deleted)")
        except Exception as exc:
            print(f"listenKey    : FAIL {exc}")
            return 3
    print("== probe OK ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

