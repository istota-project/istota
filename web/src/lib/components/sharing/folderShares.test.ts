import { describe, expect, it } from 'vitest';
import { shareCaption, sharedFrom } from './folderShares';

describe('sharedFrom', () => {
  it('names the owner by display name and federated id', () => {
    expect(sharedFrom({ owner: 'carol@cloud.example.org', owner_display_name: 'Carol' })).toBe(
      'Carol (carol@cloud.example.org)',
    );
  });

  it('puts the sharer first when it is not the owner', () => {
    expect(
      sharedFrom({
        owner: 'carol@cloud.example.org',
        owner_display_name: 'Carol',
        shared_by: 'dave@cloud.example.org',
        shared_by_display_name: null,
      }),
    ).toBe('dave@cloud.example.org, for Carol (carol@cloud.example.org)');
  });

  it('is empty when the sender named nobody', () => {
    expect(sharedFrom({ owner: null })).toBe('');
  });
});

describe('shareCaption', () => {
  it('says file or folder once known, then who and where', () => {
    expect(shareCaption({ kind: 'file', owner: 'carol@x.org', host: 'x.org' })).toBe(
      'File · from carol@x.org · x.org',
    );
    expect(shareCaption({ kind: null, owner: null, host: 'x.org' })).toBe('x.org');
  });
});
