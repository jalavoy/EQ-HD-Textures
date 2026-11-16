/* EQ HD Textures.exe: starts the window from the folder it's in.
 *
 *   <folder>\EQ HD Textures.exe            this program
 *   <folder>\runtime\python\pythonw.exe    the bundled Python (no console)
 *   <folder>\runtime\app\                  the app (working directory)
 *
 * Runs `pythonw.exe -m eq_hd.gui.app <its own arguments>` and exits.
 * `--check` only reports whether the runtime is there (exit 0, or 3), without
 * any window: for tests and the release check. Built by build.py (Zig).
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

#define TITLE L"EQ HD Textures"
#define MISSING 3

static const wchar_t *args_after_program(const wchar_t *cmd) {
    if (*cmd == L'"') {                     /* "C:\path with spaces\EQ HD Textures.exe" args */
        cmd++;
        while (*cmd && *cmd != L'"') cmd++;
        if (*cmd) cmd++;
    } else {
        while (*cmd && *cmd != L' ' && *cmd != L'\t') cmd++;
    }
    while (*cmd == L' ' || *cmd == L'\t') cmd++;
    return cmd;
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR cmdline, int show) {
    (void)inst; (void)prev; (void)cmdline; (void)show;
    static wchar_t dir[32768], python[32768], workdir[32768], command[65536];

    DWORD n = GetModuleFileNameW(NULL, dir, 32768);
    if (n == 0 || n >= 32768) return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;

    const wchar_t *args = args_after_program(GetCommandLineW());
    int check = wcscmp(args, L"--check") == 0;

    _snwprintf(python, 32768, L"%ls\\runtime\\python\\pythonw.exe", dir);
    _snwprintf(workdir, 32768, L"%ls\\runtime\\app", dir);
    if (GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES) {
        if (!check)
            MessageBoxW(NULL,
                L"Extract the whole folder first (right-click the zip \x2192 Extract All), "
                L"then run EQ HD Textures.exe from the extracted folder.",
                TITLE, MB_OK | MB_ICONINFORMATION);
        return MISSING;
    }
    if (check) return 0;

    _snwprintf(command, 65536, L"\"%ls\" -m eq_hd.gui.app %ls", python, args);
    STARTUPINFOW si = {0};
    PROCESS_INFORMATION pi = {0};
    si.cb = sizeof si;
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, 0, NULL, workdir, &si, &pi)) {
        wchar_t msg[512];
        _snwprintf(msg, 512, L"Couldn't start EQ HD Textures (Windows error %lu). "
                             L"Try extracting the zip again.", GetLastError());
        MessageBoxW(NULL, msg, TITLE, MB_OK | MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
