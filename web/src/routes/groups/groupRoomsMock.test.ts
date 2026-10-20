import { expect, it } from 'vitest';
import { Readable } from 'node:stream';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { ViteDevServer } from 'vite';
import { mockApi } from '../../../vite-mock-api';

function mockServer() {
  let middleware: (req: IncomingMessage, res: ServerResponse, next: () => void) => void;
  const plugin = mockApi();
  const configure = plugin.configureServer as (server: ViteDevServer) => void;
  configure({
    middlewares: {
      use: (handler: typeof middleware) => {
        middleware = handler;
      },
    },
  } as ViteDevServer);
  return function request(method: string, path: string, body?: unknown) {
    return new Promise<{ status: number; body: any }>((resolve) => {
      const req = Readable.from(body ? [Buffer.from(JSON.stringify(body))] : []) as IncomingMessage;
      req.method = method;
      req.url = `/istota/api${path}`;
      const res = {
        statusCode: 200,
        setHeader() {},
        end(raw: string) {
          resolve({ status: res.statusCode, body: raw.startsWith('{') ? JSON.parse(raw) : raw });
        },
      } as unknown as ServerResponse;
      middleware(req, res, () => resolve({ status: 404, body: null }));
    });
  };
}

it('creates a group room once per request id and lists it in Groups and Chat', async () => {
  const request = mockServer();
  const listed = await request('GET', '/groups/team/rooms');
  expect(listed.status).toBe(200);
  expect(listed.body.can_create).toBe(true);
  const planning = listed.body.rooms.find((r: any) => r.name === 'Planning');
  expect(planning).toMatchObject({ membership_source: 'group' });
  expect(typeof planning.room_id).toBe('number');

  const { revision } = (await request('GET', '/groups/team')).body;
  const created = await request('POST', '/groups/team/rooms', {
    name: '  Retro ',
    request_id: 'req-retro-0001',
    revision,
  });
  expect(created.status).toBe(201);
  expect(created.body).toMatchObject({
    group_id: 'team',
    created: true,
    revision: revision + 1,
    room: { name: 'Retro', origin: 'web', membership_source: 'group' },
  });
  const roomId = created.body.room.room_id;
  expect(typeof roomId).toBe('number');

  // A replay answers with the same room, whatever revision it carries.
  const replay = await request('POST', '/groups/team/rooms', {
    name: 'Retro',
    request_id: 'req-retro-0001',
    revision,
  });
  expect(replay.status).toBe(200);
  expect(replay.body).toMatchObject({ created: false, revision: revision + 1 });
  expect(replay.body.room.room_id).toBe(roomId);

  // The same request id for another name is refused.
  const mismatch = await request('POST', '/groups/team/rooms', {
    name: 'Something else',
    request_id: 'req-retro-0001',
    revision: revision + 1,
  });
  expect(mismatch.status).toBe(409);
  expect(mismatch.body.error).toBe('request_conflict');

  // A new request written against the old room list is a revision conflict.
  const stale = await request('POST', '/groups/team/rooms', {
    name: 'Later',
    request_id: 'req-later-0001',
    revision,
  });
  expect(stale.status).toBe(409);
  expect(stale.body).toMatchObject({ error: 'revision_conflict', revision: revision + 1 });

  const detail = (await request('GET', '/groups/team')).body;
  expect(detail.revision).toBe(revision + 1);
  expect(detail.rooms.filter((r: any) => r.room_id === roomId)).toHaveLength(1);

  const chat = (await request('GET', '/chat/rooms')).body.rooms;
  const retro = chat.find((r: any) => r.id === roomId);
  expect(retro.managed_by).toEqual({
    group_id: 'team',
    display_name: 'Team',
    manage_url: '/istota/groups/?group=team',
    is_owner: true,
  });
  // Every chat room keeps its own id.
  expect(new Set(chat.map((r: any) => r.id)).size).toBe(chat.length);
});

it('serves a group room the caller does not own, locked to the group', async () => {
  const request = mockServer();
  const chat = (await request('GET', '/chat/rooms')).body.rooms;
  const owned = chat.find((r: any) => r.managed_by?.group_id === 'team');
  expect(owned.managed_by.is_owner).toBe(true);
  expect(owned.policy.settings_refusal).toBeNull();

  const club = chat.find((r: any) => r.managed_by?.group_id === 'reading-circle');
  expect(club.managed_by).toMatchObject({ display_name: 'Reading circle', is_owner: false });
  expect(club.policy.settings_refusal).toBe(
    "Only an owner of group 'reading-circle' can change this room's settings.",
  );

  const members = (await request('GET', `/chat/rooms/${club.id}/members`)).body;
  expect(members.can_manage).toBe(false);
  expect(members.managed_by).toEqual(club.managed_by);

  const add = await request('POST', `/chat/rooms/${club.id}/members`, {
    user_id: 'erin',
    acknowledge_history: true,
  });
  expect(add.status).toBe(409);
  expect(add.body.reason).toBe('group_managed');

  const rename = await request('PATCH', `/chat/rooms/${club.id}`, { name: 'Novels' });
  expect(rename.status).toBe(403);

  const promote = await request('POST', `/chat/rooms/${club.id}/promote`);
  expect(promote.status).toBe(409);
  expect(promote.body.reason).toBe('group_managed');

  // The Groups page's cards for this group load rather than 404.
  expect((await request('GET', '/groups/reading-circle')).status).toBe(200);
  expect((await request('GET', '/groups/reading-circle/memory')).status).toBe(200);

  const create = await request('POST', '/groups/reading-circle/rooms', {
    name: 'Picks',
    request_id: 'req-picks-0001',
    revision: 1,
  });
  expect(create.status).toBe(403);
  expect(create.body.error).toBe('group_forbidden');
  expect((await request('GET', '/groups/reading-circle/rooms')).body.can_create).toBe(false);
});
