{ pkgs, ... }:

{
  dotenv.enable = true;
  packages = [ pkgs.git pkgs.ffmpeg ];

  languages.python = {
    enable = true;
    uv.enable = true;
    uv.sync.enable = true;
    venv.enable = true;
  };

  enterShell = ''
    python --version
    uv --version
  '';

  enterTest = ''
    uv run python -m unittest discover -s tests
    uv run ruff check .
  '';
}
