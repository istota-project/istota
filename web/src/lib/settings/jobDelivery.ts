/**
 * A scheduled job's `target` and `room` as the form's output and room, and back.
 *
 * A Talk room is the job's `room`, as CRON.md writes it, or a `talk:<token>` leaf. A web
 * room is the leaf, since the server sets `room` from a `web:` target. Anything that is
 * not one roomed leaf stays whole as the output: no control could put its other half back.
 */
import { joinDescriptor, routeRoom, routeSurface } from '$lib/deliveryDescriptor';

export function deliveryFromJob(target: string, room: string): { output: string; room: string } {
  const output = routeSurface(target);
  if (output === 'talk') return { output, room: routeRoom(target) || room };
  if (output === 'web') return { output, room: routeRoom(target) };
  return { output, room: '' };
}

export function jobTarget(output: string, room: string): { target: string; room: string } {
  if (output === 'talk') return { target: 'talk', room };
  if (output === 'web') return { target: joinDescriptor('web', room), room: '' };
  return { target: output, room: '' };
}
