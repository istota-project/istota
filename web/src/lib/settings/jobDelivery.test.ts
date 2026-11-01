import { describe, expect, it } from 'vitest';
import { deliveryFromJob, jobTarget } from './jobDelivery';

describe('deliveryFromJob', () => {
  it('reads a Talk room from the job or from the descriptor', () => {
    expect(deliveryFromJob('talk', 'tk1')).toEqual({ output: 'talk', room: 'tk1' });
    expect(deliveryFromJob('talk:tk1', '')).toEqual({ output: 'talk', room: 'tk1' });
  });

  it('reads a web room from the descriptor, not the room the server set', () => {
    expect(deliveryFromJob('web:rm1', 'rm1')).toEqual({ output: 'web', room: 'rm1' });
    expect(deliveryFromJob('web', '')).toEqual({ output: 'web', room: '' });
  });

  it('keeps anything else whole, since no control could put it back', () => {
    expect(deliveryFromJob('email', '')).toEqual({ output: 'email', room: '' });
    expect(deliveryFromJob('web:rm1,talk:tk1', 'rm1')).toEqual({
      output: 'web:rm1,talk:tk1',
      room: '',
    });
    expect(deliveryFromJob('room:rm1', 'rm1')).toEqual({ output: 'room:rm1', room: '' });
    expect(deliveryFromJob('', '')).toEqual({ output: '', room: '' });
  });
});

describe('jobTarget', () => {
  it('sends a Talk room as the room, as CRON.md writes it', () => {
    expect(jobTarget('talk', 'tk1')).toEqual({ target: 'talk', room: 'tk1' });
  });

  it('sends a web room in the descriptor, and the server sets the room', () => {
    expect(jobTarget('web', 'rm1')).toEqual({ target: 'web:rm1', room: '' });
    expect(jobTarget('web', '')).toEqual({ target: 'web', room: '' });
  });

  it('sends anything else whole', () => {
    expect(jobTarget('email', '')).toEqual({ target: 'email', room: '' });
    expect(jobTarget('web:rm1,talk:tk1', '')).toEqual({ target: 'web:rm1,talk:tk1', room: '' });
  });
});
