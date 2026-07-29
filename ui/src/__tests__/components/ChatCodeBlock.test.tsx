/**
 * Tests for ChatCodeBlock — fenced code rendering + download filename derivation.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import { ChatCodeBlock, deriveFileName } from '../../components/ChatCodeBlock';

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

  it('maps common languages to their extension', () => {
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

describe('ChatCodeBlock', () => {
  it('renders inline code as a simple element', () => {
    render(<ChatCodeBlock inline>fleetId</ChatCodeBlock>);
    const el = screen.getByText('fleetId');
    expect(el.tagName).toBe('CODE');
    expect(el).toHaveClass('ga-inline-code');
  });

  it('renders a fenced block with the language label and action buttons', () => {
    render(
      <ChatCodeBlock className="language-main.tf">{'resource "aws_gamelift_fleet" "x" {}\n'}</ChatCodeBlock>
    );
    expect(screen.getByText('main.tf')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /download main\.tf/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /copy code/i })).toBeInTheDocument();
  });

  it('downloads using the derived filename from the fence token', () => {
    const createUrl = jest.fn(() => 'blob:mock');
    const revokeUrl = jest.fn();
    // jsdom lacks these; provide mocks.
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
