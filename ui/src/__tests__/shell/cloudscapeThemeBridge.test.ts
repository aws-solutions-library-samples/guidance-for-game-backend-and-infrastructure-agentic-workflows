/**
 * The theme bridge drives Cloudscape's visual mode from the already-resolved
 * theme and redefines the shared `--ga-*` custom properties from design-token
 * values. It MUST map `light`/`dark` to the matching Cloudscape mode and MUST
 * write the token values rather than hard-coded colors, so a token-package
 * upgrade flows through automatically.
 */

import { applyMode, Mode } from '@cloudscape-design/global-styles';
import * as tokens from '@cloudscape-design/design-tokens';
import { applyCloudscapeMode, applyGaTokenBridge } from '@/shell/cloudscapeThemeBridge';

jest.mock('@cloudscape-design/global-styles', () => ({
  Mode: { Light: 'light', Dark: 'dark' },
  applyMode: jest.fn(),
}));

describe('applyCloudscapeMode', () => {
  beforeEach(() => {
    (applyMode as jest.Mock).mockClear();
  });

  it('applies the light visual mode for the light theme', () => {
    applyCloudscapeMode('light');
    expect(applyMode).toHaveBeenCalledWith(Mode.Light, document.documentElement);
  });

  it('applies the dark visual mode for the dark theme', () => {
    applyCloudscapeMode('dark');
    expect(applyMode).toHaveBeenCalledWith(Mode.Dark, document.documentElement);
  });
});

describe('applyGaTokenBridge', () => {
  it('writes --ga-* properties from design-token values, not hard-coded colors', () => {
    const root = document.createElement('div');
    applyGaTokenBridge(root);

    // The token package returns hashed custom-property references; the bridge
    // must pass those through verbatim rather than substituting a literal color.
    expect(root.style.getPropertyValue('--ga-text')).toBe(tokens.colorTextBodyDefault);
    expect(root.style.getPropertyValue('--ga-bg')).toBe(tokens.colorBackgroundLayoutMain);
    expect(root.style.getPropertyValue('--ga-accent')).toBe(tokens.colorTextInteractiveDefault);
    expect(root.style.getPropertyValue('--ga-text')).toMatch(/^var\(--/);
  });

  it('is a no-op for a missing root', () => {
    expect(() => applyGaTokenBridge(null)).not.toThrow();
  });
});
