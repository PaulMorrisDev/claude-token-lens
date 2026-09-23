"""Which files a shell command writes (``shell_writes``): only content the
command authored counts, and relative paths resolve against where it ran."""

from __future__ import annotations

import pytest

from claude_token_lens.shell_writes import write_targets

CWD = "C:\\Dev\\app"


@pytest.mark.parametrize("command, expected", [
    # A heredoc's body is skipped, quotes and all.
    ("cat > src/a.py <<'EOF'\nx = \"it's\" > 1\necho no > nope.txt\nEOF\necho done", ["C:/Dev/app/src/a.py"]),
    ("cat <<EOF > /c/Dev/app/a.txt\nhi\nEOF", ["/c/Dev/app/a.txt"]),
    ('echo "x" >> notes.md', ["C:/Dev/app/notes.md"]),
    ('echo x > "C:\\Dev\\app\\a b.txt"', ["C:/Dev/app/a b.txt"]),
    ("sed -i 's/a/b/' src/a.py src/b.py", ["C:/Dev/app/src/a.py", "C:/Dev/app/src/b.py"]),
    ("sed -i.bak -e 's/a/b/' -e 's/c/d/' a.py", ["C:/Dev/app/a.py"]),
    ("sed -i '' 's/a/b/' a.py", ["C:/Dev/app/a.py"]),
    ("perl -pi -e 's/a/b/' a.py", ["C:/Dev/app/a.py"]),
    ("printf 'a\\n' | tee -a out.txt", ["C:/Dev/app/out.txt"]),
    ("cd sub && echo x > f.txt", ["C:/Dev/app/sub/f.txt"]),
    ('x=$(cat a) && echo "$x" > ../b.txt', ["C:/Dev/app/../b.txt"]),
    # Capturing a program's output is a log, not an edit.
    ("npm test > test.log 2>&1", []),
    ("npm test 2>&1 | tee log.txt", []),
    ("pytest 2> err.txt", []),
    ("sed 's/a/b/' a.py > b.py", []),
    ("perl -ne 'print if /x/' a.py", []),
    # Devices, variables, quoted text, comments and unknown directories.
    ("echo hi > /dev/null", []),
    ("echo x > $OUT", []),
    ('git commit -m "fix > bug"', []),
    ("# echo x > f\nls", []),
    ("cd $DIR && echo x > f.txt", []),
])
def test_bash(command, expected):
    assert write_targets(command, powershell=False, cwd=CWD) == expected


@pytest.mark.parametrize("command, expected", [
    ("Set-Content -Path src\\a.ts -Value $c -Encoding utf8", ["C:/Dev/app/src/a.ts"]),
    ('$c | Set-Content "C:\\Dev\\app\\a.ts" -NoNewline', ["C:/Dev/app/a.ts"]),
    ("(Get-Content a.ts) -replace 'x','y' | Set-Content a.ts", ["C:/Dev/app/a.ts"]),
    ("@'\nline > 1\n'@ | Out-File -FilePath notes.md -Encoding utf8", ["C:/Dev/app/notes.md"]),
    ("[IO.File]::WriteAllText('C:\\Dev\\app\\x.json', $json)", ["C:/Dev/app/x.json"]),
    ("Set-Location sub; Add-Content -LiteralPath log.md -Value 'x'", ["C:/Dev/app/sub/log.md"]),
    ('"text" > out.txt', ["C:/Dev/app/out.txt"]),
    ("if (Test-Path a.md) { Set-Content a.md 'y' }", ["C:/Dev/app/a.md"]),
    ("npm test | Out-File log.txt", []),
    ("npm run build *> build.log", []),
    # .NET resolves against the process directory, not the shell's.
    ('[System.IO.File]::WriteAllText("rel.json", $json)', []),
    ("$p = Join-Path $root 'x.md'; Set-Content $p 'y'", []),
    ("Get-Content a.md 2>$null", []),
])
def test_powershell(command, expected):
    assert write_targets(command, powershell=True, cwd=CWD) == expected


def test_a_relative_path_with_no_known_directory_is_skipped():
    assert write_targets("echo x > rel.txt", powershell=False, cwd=None) == []
    assert write_targets("echo x > /c/Dev/app/abs.txt", powershell=False, cwd=None) == ["/c/Dev/app/abs.txt"]
