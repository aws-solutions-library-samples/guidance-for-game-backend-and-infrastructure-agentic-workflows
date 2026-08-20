/**
 * Tests for ChatCodeBlock — fenced code rendering, block/inline classification,
 * syntax highlighting, download filename derivation, and language mapping.
 * Covers the regressions raised in review of PR #264.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import {
  ChatCodeBlock,
  deriveFileName,
  deriveHighlightLanguage,
} from '../../components/ChatCodeBlock';

describe('deriveFileName', () => {
  it('uses a filename-like fence token verbatim so download matches the UI label', () => {
    expect(deriveFileName('main.tf')).toBe('main.tf');
    expect(deriveFileName('variables.tf')).toBe('variables.tf');
  });

  it('maps IaC language tokens to a terraform extension', () => {
    expect(deriveFileName('hcl')).toBe('code.tf');
    expect(deriveFileName('terraform')).toBe('code.tf');
    expect(deriveFileName('tf')).toBe('code.tf');
  });

  it('preserves CopilotKit’s existing extension mappings (no regression to .txt)', () => {
    expect(deriveFileName('cpp')).toBe('code.cpp');
    expect(deriveFileName('c++')).toBe('code.cpp');
    expect(deriveFileName('c#')).toBe('code.cs');
    expect(deriveFileName('php')).toBe('code.php');
    expect(deriveFileName('kotlin')).toBe('code.kt');
    expect(deriveFileName('swift')).toBe('code.swift');
    expect(deriveFileName('objective-c')).toBe('code.m');
    expect(deriveFileName('perl')).toBe('code.pl');
    expect(deriveFileName('scala')).toBe('code.scala');
    expect(deriveFileName('haskell')).toBe('code.hs');
  });

  it('maps other common languages to their extension', () => {
    expect(deriveFileName('json')).toBe('code.json');
    expect(deriveFileName('yaml')).toBe('code.yaml');
    expect(deriveFileName('python')).toBe('code.py');
  });

  it('special-cases Dockerfile and falls back for unknown tokens', () => {
    expect(deriveFileName('dockerfile')).toBe('Dockerfile');
    expect(deriveFileName('unknownlang')).toBe('unknownlang.txt');
    expect(deriveFileName('')).toBe('code.txt');
  });
});

describe('deriveHighlightLanguage', () => {
  it('reduces filename tokens to a prism language by extension', () => {
    expect(deriveHighlightLanguage('main.tf')).toBe('hcl');
    expect(deriveHighlightLanguage('app.py')).toBe('python');
  });

  it('normalizes language aliases and defaults to text', () => {
    expect(deriveHighlightLanguage('tf')).toBe('hcl');
    expect(deriveHighlightLanguage('py')).toBe('python');
    expect(deriveHighlightLanguage('')).toBe('text');
  });
});

describe('ChatCodeBlock', () => {
  it('renders inline code as a simple element', () => {
    render(<ChatCodeBlock inline>fleetId</ChatCodeBlock>);
    const el = screen.getByText('fleetId');
    expect(el.tagName).toBe('CODE');
    expect(el).toHaveClass('ga-inline-code');
  });

  it('classifies a one-line UNLABELED fenced block as a block (keeps download control)', () => {
    // react-markdown supplies one-line fenced blocks with a trailing newline and
    // no language class. This must NOT be treated as inline.
    render(<ChatCodeBlock>{'echo hello\n'}</ChatCodeBlock>);
    expect(screen.getByRole('button', { name: /download code\.txt/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /copy code/i })).toBeInTheDocument();
  });

  it('treats truly inline content (no language, no newline) as inline', () => {
    render(<ChatCodeBlock>{'aws eks list-clusters'}</ChatCodeBlock>);
    expect(screen.queryByRole('button', { name: /download/i })).not.toBeInTheDocument();
    expect(screen.getByText('aws eks list-clusters')).toHaveClass('ga-inline-code');
  });

  it('renders a fenced block with the language label and action buttons', () => {
    render(
      <ChatCodeBlock className="language-main.tf">{'resource "aws_gamelift_fleet" "x" {}\n'}</ChatCodeBlock>
    );
    expect(screen.getByText('main.tf')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /download main\.tf/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /copy code/i })).toBeInTheDocument();
  });

  it('applies syntax highlighting (token spans) for a labeled block', () => {
    const { container } = render(
      <ChatCodeBlock className="language-python">{'def handler():\n    return 1\n'}</ChatCodeBlock>
    );
    // react-syntax-highlighter (Prism) emits <span class="token ..."> for
    // recognized languages; its presence confirms highlighting is preserved.
    expect(container.querySelector('span[class*="token"]')).not.toBeNull();
  });

  it('emits classless (non-token) spans inside the pre that the light-mode contrast rule targets', () => {
    // Regression guard for the #252 follow-up: Prism wraps unclassified code
    // fragments (e.g. Python parameter names) in classless <span>s. The CSS rule
    // `.copilotKitMarkdown .ga-code-block__pre span:not([class*="token"])` must
    // re-color those to a light value, or they inherit the near-black
    // var(--ga-text) and vanish on the dark code surface in light mode.
    // jsdom does not apply stylesheet cascade/specificity, so we cannot assert
    // computed color here; instead we lock in the DOM structure the rule keys
    // off — a classless span living under .ga-code-block__pre. If highlighting
    // ever stops producing these, this test flags that the CSS scope needs a
    // second look.
    const { container } = render(
      <ChatCodeBlock className="language-python">{'def handler(event, context):\n    return event\n'}</ChatCodeBlock>
    );
    const pre = container.querySelector('.ga-code-block__pre');
    expect(pre).not.toBeNull();
    const spans = Array.from(pre!.querySelectorAll('span'));
    const classlessSpans = spans.filter(
      (s) => !Array.from(s.classList).some((c) => c.includes('token'))
    );
    expect(classlessSpans.length).toBeGreaterThan(0);
  });

  it('downloads using the derived filename from the fence token', () => {
    const createUrl = jest.fn(() => 'blob:mock');
    const revokeUrl = jest.fn();
    Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: revokeUrl });

    const clickSpy = jest
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    let capturedDownload = '';
    const setAttrSpy = jest
      .spyOn(HTMLAnchorElement.prototype, 'download', 'set')
      .mockImplementation(function (this: HTMLAnchorElement, v: string) {
        capturedDownload = v;
      });

    render(
      <ChatCodeBlock className="language-main.tf">{'terraform {}\n'}</ChatCodeBlock>
    );
    fireEvent.click(screen.getByRole('button', { name: /download main\.tf/i }));

    expect(createUrl).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(capturedDownload).toBe('main.tf');

    clickSpy.mockRestore();
    setAttrSpy.mockRestore();
  });
});
