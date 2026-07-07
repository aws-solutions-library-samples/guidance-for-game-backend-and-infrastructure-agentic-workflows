import { ThemeMode, useTheme } from './ThemeProvider';

const themeOptions: Array<{ mode: ThemeMode; label: string; title: string }> = [
  { mode: 'light', label: 'Light', title: 'Use light theme' },
  { mode: 'dark', label: 'Dark', title: 'Use dark theme' },
  { mode: 'system', label: 'System', title: 'Follow your operating system theme' },
];

export default function ThemeToggle() {
  const { mode, resolvedTheme, setMode } = useTheme();

  return (
    <div
      className="ga-theme-toggle"
      role="group"
      aria-label={`Theme preference, currently ${mode}${mode === 'system' ? ` (${resolvedTheme})` : ''}`}
    >
      {themeOptions.map(option => (
        <button
          key={option.mode}
          type="button"
          className={mode === option.mode ? 'active' : ''}
          aria-pressed={mode === option.mode}
          title={option.title}
          onClick={() => setMode(option.mode)}
        >
          {option.label}
        </button>
      ))}

      <style jsx>{`
        .ga-theme-toggle {
          display: inline-flex;
          align-items: center;
          gap: 2px;
          padding: 4px;
          background: var(--ga-segment-bg);
          border: 1px solid var(--ga-border);
          border-radius: 8px;
          box-shadow: var(--ga-small-shadow);
          flex-shrink: 0;
        }

        .ga-theme-toggle button {
          min-width: 58px;
          height: 30px;
          padding: 0 10px;
          border-radius: 6px;
          color: var(--ga-text-muted);
          font-size: 12px;
          font-weight: 600;
          line-height: 1;
          transition: background 0.2s ease, color 0.2s ease, box-shadow 0.2s ease;
          white-space: nowrap;
        }

        .ga-theme-toggle button:hover {
          color: var(--ga-text);
          background: var(--ga-control-hover-bg);
        }

        .ga-theme-toggle button.active {
          color: var(--ga-control-active-text);
          background: var(--ga-control-active-bg);
          box-shadow: var(--ga-control-active-shadow);
        }

        @media (max-width: 640px) {
          .ga-theme-toggle {
            order: -1;
          }

          .ga-theme-toggle button {
            min-width: 44px;
            padding: 0 8px;
            font-size: 11px;
          }
        }
      `}</style>
    </div>
  );
}
