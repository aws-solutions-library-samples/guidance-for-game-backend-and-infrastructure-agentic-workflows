/**
 * The class `_document` writes the per-page shell into the server HTML so the
 * correct shell's scoped CSS applies at first paint. It MUST read the rendered
 * page from `__NEXT_DATA__.page` and set `data-ga-shell` on `<Html>`, defaulting
 * to the current shell for every page except the Cloudscape entry.
 */

import type { ReactElement } from 'react';
import GameAgentDocument from '@/pages/_document';
import { CLOUDSCAPE_PAGE } from '@/shell/shell';

/** Render the document class with a given page and read the <Html> props. */
function htmlPropsForPage(page: string): Record<string, unknown> {
  // The real Next runtime passes a rich props object; the document only reads
  // __NEXT_DATA__.page, so a minimal stand-in is sufficient here.
  const instance = new GameAgentDocument({ __NEXT_DATA__: { page } } as never);
  const tree = instance.render() as ReactElement<Record<string, unknown>>;
  return tree.props;
}

describe('_document data-ga-shell', () => {
  it('marks the Cloudscape entry page with the cloudscape shell', () => {
    expect(htmlPropsForPage(CLOUDSCAPE_PAGE)['data-ga-shell']).toBe('cloudscape');
  });

  it.each(['/', '/admin/users', '/404'])('marks %s with the current shell', (page) => {
    expect(htmlPropsForPage(page)['data-ga-shell']).toBe('current');
  });

  it('is a class component (so __NEXT_DATA__ is available at render time)', () => {
    expect(GameAgentDocument.prototype.render).toBeInstanceOf(Function);
  });
});
