import { createContext, useCallback, useContext, useEffect, useMemo, useSyncExternalStore } from 'react';
import type { ReactNode } from 'react';

export type ThemeMode = 'light' | 'dark' | 'system';
type ResolvedTheme = 'light' | 'dark';
type ThemeSnapshot = `${ThemeMode}:${ResolvedTheme}`;

interface ThemeContextValue {
  mode: ThemeMode;
  resolvedTheme: ResolvedTheme;
  setMode: (mode: ThemeMode) => void;
}

const THEME_STORAGE_KEY = 'game-agent-theme';
const THEME_CHANGE_EVENT = 'game-agent-theme-change';
const SERVER_THEME_SNAPSHOT: ThemeSnapshot = 'system:dark';
const ThemeContext = createContext<ThemeContextValue | undefined>(undefined);
let fallbackThemeMode: ThemeMode = 'system';

function getSystemTheme(): ResolvedTheme {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return 'dark';
  }

  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function normalizeThemeMode(value: string | null): ThemeMode {
  return value === 'light' || value === 'dark' || value === 'system' ? value : 'system';
}

function getStoredThemeMode(): ThemeMode {
  if (typeof window === 'undefined') {
    return 'system';
  }

  try {
    fallbackThemeMode = normalizeThemeMode(window.localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    // Keep the in-memory preference for environments where storage is blocked.
  }

  return fallbackThemeMode;
}

function getThemeSnapshot(): ThemeSnapshot {
  const mode = getStoredThemeMode();
  const systemTheme = getSystemTheme();
  const resolvedTheme = mode === 'system' ? systemTheme : mode;

  return `${mode}:${resolvedTheme}`;
}

function getServerThemeSnapshot(): ThemeSnapshot {
  return SERVER_THEME_SNAPSHOT;
}

function parseThemeSnapshot(snapshot: ThemeSnapshot) {
  const [mode, resolvedTheme] = snapshot.split(':') as [ThemeMode, ResolvedTheme];
  return { mode, resolvedTheme };
}

function applyTheme(mode: ThemeMode, resolvedTheme: ResolvedTheme) {
  if (typeof document === 'undefined') {
    return;
  }

  document.documentElement.dataset.themeMode = mode;
  document.documentElement.dataset.theme = resolvedTheme;
  document.documentElement.style.colorScheme = resolvedTheme;
}

function subscribeToThemeChanges(onStoreChange: () => void) {
  if (typeof window === 'undefined') {
    return () => {};
  }

  const handleStorage = (event: StorageEvent) => {
    if (event.key === THEME_STORAGE_KEY) {
      fallbackThemeMode = normalizeThemeMode(event.newValue);
      onStoreChange();
    }
  };
  const handleThemeChange = () => onStoreChange();

  window.addEventListener('storage', handleStorage);
  window.addEventListener(THEME_CHANGE_EVENT, handleThemeChange);

  if (typeof window.matchMedia !== 'function') {
    return () => {
      window.removeEventListener('storage', handleStorage);
      window.removeEventListener(THEME_CHANGE_EVENT, handleThemeChange);
    };
  }

  const mediaQuery = window.matchMedia('(prefers-color-scheme: light)');

  if (typeof mediaQuery.addEventListener === 'function') {
    mediaQuery.addEventListener('change', onStoreChange);
    return () => {
      window.removeEventListener('storage', handleStorage);
      window.removeEventListener(THEME_CHANGE_EVENT, handleThemeChange);
      mediaQuery.removeEventListener('change', onStoreChange);
    };
  }

  mediaQuery.addListener(onStoreChange);
  return () => {
    window.removeEventListener('storage', handleStorage);
    window.removeEventListener(THEME_CHANGE_EVENT, handleThemeChange);
    mediaQuery.removeListener(onStoreChange);
  };
}

function notifyThemeChange() {
  window.dispatchEvent(new Event(THEME_CHANGE_EVENT));
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const snapshot = useSyncExternalStore(
    subscribeToThemeChanges,
    getThemeSnapshot,
    getServerThemeSnapshot,
  );
  const { mode, resolvedTheme } = useMemo(() => parseThemeSnapshot(snapshot), [snapshot]);

  useEffect(() => {
    applyTheme(mode, resolvedTheme);
  }, [mode, resolvedTheme]);

  const setMode = useCallback((nextMode: ThemeMode) => {
    fallbackThemeMode = nextMode;

    if (typeof window === 'undefined') {
      return;
    }

    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, nextMode);
    } catch {
      // Theme selection still works for this session if storage is unavailable.
    }

    notifyThemeChange();
  }, []);

  const value = useMemo(
    () => ({
      mode,
      resolvedTheme,
      setMode,
    }),
    [mode, resolvedTheme, setMode],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const value = useContext(ThemeContext);

  if (!value) {
    throw new Error('useTheme must be used within ThemeProvider');
  }

  return value;
}
