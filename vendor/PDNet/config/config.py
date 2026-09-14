import argparse
import yaml
import os

def get_parser():
    parser = argparse.ArgumentParser(description='IMAGE_DEMOIREING')
    parser.add_argument('--config', type=str, default='config/uhdm_config.yaml', help='path to config file')
    args_cfg = parser.parse_args()
    assert args_cfg.config is not None
    # COMPAT: open() without an explicit encoding uses the platform locale
    # (GBK on Chinese Windows), which raises UnicodeDecodeError on any config
    # containing non-ASCII characters such as Chinese comments.
    with open(args_cfg.config, 'r', encoding='utf-8') as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    for key in config:
        # COMPAT: the original code assumed every top-level key is a mapping
        # (GENERAL/DATA/MODEL/...). A top-level scalar such as
        #     USE_TEACHER: False
        # would raise AttributeError: 'bool' object has no attribute 'items'.
        # Skip non-mappings instead of crashing.
        value = config[key]
        if not isinstance(value, dict):
            setattr(args_cfg, key, value)
            continue
        for k, v in value.items():
            setattr(args_cfg, k, v)

    return args_cfg


args = get_parser()
