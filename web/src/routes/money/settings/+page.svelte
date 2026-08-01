<script lang="ts">
  import { monarchLogin } from '$lib/api';
  import { getMoneyServices } from '$lib/money/settingsContext';
  import {
    ServiceCard,
    SettingsLayout,
    SettingsCard,
    SettingsField,
  } from '$lib/components/settings';
  import { Button, HintPopover, Input } from '$lib/components/ui';

  const moneyServices = getMoneyServices();

  // Programmatic-login form state. Plain bindings — values are POSTed to
  // /money/monarch/login and never persisted in the browser beyond the
  // in-memory component state.
  let loginEmail = $state('');
  let loginPassword = $state('');
  let loginMfa = $state('');
  let loginBusy = $state(false);
  let loginMessage = $state('');
  let loginErrorKind = $state<
    '' | 'auth' | 'mfa' | 'cloudflare' | 'captcha' | 'other' | 'challenge'
  >('');
  // A pending one-time-code step. Non-empty means the password was *accepted*
  // and Monarch is waiting on a code, which is a different thing from a failed
  // login and has to look different.
  let loginChallenge = $state<'' | 'email_otp' | 'mfa'>('');
  let loginCode = $state('');

  function resetLogin() {
    loginChallenge = '';
    loginCode = '';
    loginPassword = '';
    loginMfa = '';
    loginMessage = '';
    loginErrorKind = '';
  }

  async function submitLogin() {
    if (!loginEmail || !loginPassword) return;
    if (loginChallenge && !loginCode) return;
    loginBusy = true;
    loginMessage = '';
    loginErrorKind = '';
    // On the code step the field carries whichever challenge is live; before
    // it, the optional MFA box lets someone with an authenticator skip a round
    // trip. Sending both would be wrong — they are different credentials.
    const codes =
      loginChallenge === 'email_otp'
        ? { emailOtp: loginCode }
        : loginChallenge === 'mfa'
          ? { mfaTotp: loginCode }
          : { mfaTotp: loginMfa };
    try {
      const result = await monarchLogin(loginEmail, loginPassword, codes);
      if (result.status === 'ok') {
        resetLogin();
        loginMessage = 'Logged in — session_id and csrftoken saved.';
        loginErrorKind = '';
        await moneyServices.reload();
        return;
      }
      if (result.status === 'challenge') {
        // Re-issuing the same challenge means the code we just sent was
        // refused. Say so, rather than silently re-rendering an identical
        // form that looks like nothing happened.
        const retry = loginChallenge === result.kind;
        loginChallenge = result.kind;
        loginCode = '';
        loginErrorKind = retry ? 'auth' : 'challenge';
        loginMessage = retry
          ? result.kind === 'email_otp'
            ? 'That code was not accepted. Codes expire — check for a newer email and try again.'
            : 'That code was not accepted. Wait for your authenticator to show a new one.'
          : result.kind === 'email_otp'
            ? `Email and password accepted. Monarch emailed a 6-digit code to ${loginEmail} — enter it below to finish.`
            : 'Email and password accepted. Enter the 6-digit code from your authenticator app to finish.';
        return;
      }
      // A real failure: drop the code step, since the password itself is now
      // in question and re-sending a code against it would only waste one.
      loginChallenge = '';
      loginCode = '';
      loginErrorKind = result.kind === 'blocked' ? 'other' : result.kind;
      loginMessage = result.message;
    } catch (e) {
      loginChallenge = '';
      loginCode = '';
      loginErrorKind = 'other';
      loginMessage = e instanceof Error ? e.message : 'Login failed';
    } finally {
      loginBusy = false;
    }
  }
</script>

<SettingsLayout
  title="Connections"
  description="Credentials for the services money reads from. Secrets are encrypted at rest and never sent back to the browser."
>
  {#each moneyServices.services as svc (svc.service)}
    {#if svc.service === 'monarch'}
      <SettingsCard
        title="Connect to Monarch Money"
        description="Monarch's API requires browser session cookies. Pick the method that works for your account."
      >
        <details class="monarch-method" open>
          <summary>
            <span class="summary-label">
              Log in with email and password
              <HintPopover
                label="About logging in with email and password"
                text="We sign in to Monarch on your behalf and store the session cookies it returns (session_id and csrftoken). Your password is used once and is never written to disk. If Monarch doesn't recognise this device it emails you a 6-digit code — enter it when asked. If Cloudflare blocks the request from this server, paste cookies from your browser instead."
              />
            </span>
          </summary>
          <form
            class="login-form"
            onsubmit={(e) => {
              e.preventDefault();
              void submitLogin();
            }}
          >
            <SettingsField label="Email">
              <Input
                type="email"
                bind:value={loginEmail}
                autocomplete="off"
                disabled={loginBusy}
                required
              />
            </SettingsField>
            <SettingsField label="Password">
              <Input
                type="password"
                bind:value={loginPassword}
                autocomplete="off"
                disabled={loginBusy}
                required
              />
            </SettingsField>
            {#if loginChallenge}
              <SettingsField
                label={loginChallenge === 'email_otp' ? 'Emailed code' : 'Authenticator code'}
              >
                <Input
                  type="text"
                  inputmode="numeric"
                  pattern="[0-9]*"
                  maxlength={6}
                  bind:value={loginCode}
                  autocomplete="one-time-code"
                  disabled={loginBusy}
                  placeholder="6-digit code"
                  autofocus
                />
              </SettingsField>
            {:else}
              <SettingsField label="MFA code" hint="Only if your account has MFA enabled.">
                <Input
                  type="text"
                  inputmode="numeric"
                  pattern="[0-9]*"
                  bind:value={loginMfa}
                  autocomplete="off"
                  disabled={loginBusy}
                  placeholder="6-digit code"
                />
              </SettingsField>
            {/if}
            <div class="login-actions">
              <Button
                variant="primary"
                size="sm"
                disabled={loginBusy ||
                  !loginEmail ||
                  !loginPassword ||
                  (!!loginChallenge && !loginCode)}
                type="submit"
              >
                {loginBusy
                  ? loginChallenge
                    ? 'Verifying…'
                    : 'Logging in…'
                  : loginChallenge
                    ? 'Verify code'
                    : 'Login & save cookies'}
              </Button>
              {#if loginChallenge}
                <Button variant="ghost" size="sm" disabled={loginBusy} onclick={resetLogin}>
                  Start over
                </Button>
              {/if}
            </div>
            {#if loginMessage}
              <div class="login-status" data-kind={loginErrorKind || 'ok'}>
                {loginMessage}
              </div>
            {/if}
          </form>
        </details>

        <details class="monarch-method">
          <summary>Paste cookies from your browser</summary>
          <p class="hint">
            Use this when programmatic login is blocked by Cloudflare (common on cloud-hosted Istota
            deploys).
          </p>
          <ol>
            <li>
              Open <a href="https://app.monarch.com" target="_blank" rel="noopener noreferrer"
                >app.monarch.com</a
              > in a logged-in browser tab.
            </li>
            <li>
              Open DevTools (Cmd/Ctrl+Option+I) → <strong>Application</strong> →
              <strong>Cookies</strong>
              → <code>https://api.monarch.com</code>.
            </li>
            <li>Copy the value of <code>session_id</code> into the field below.</li>
            <li>Copy the value of <code>csrftoken</code> into the field below.</li>
            <li>Click <strong>Save</strong>.</li>
          </ol>
        </details>

        <p class="legacy-note">
          Cookies are the only credential we store. They last months on a trusted-device login.
        </p>
      </SettingsCard>
    {/if}
    <ServiceCard service={svc} onChanged={moneyServices.reload} />
  {/each}
</SettingsLayout>

<style>
  /* The two connection methods, as disclosures inside the card. A tile on a
     card, so `--surface-raised` against the card's own fill is the tile
     contrast — the panel around them used to be a hand-rolled card at
     card level, which put a different fill and a stronger border beside every
     real SettingsCard on the page. */
  .monarch-method {
    margin: var(--space-2) 0;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-2) var(--space-3);
    background: var(--surface-raised);
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }

  .monarch-method summary {
    cursor: pointer;
    font-weight: 600;
    color: var(--text-primary);
  }

  .monarch-method p,
  .monarch-method ol {
    margin: var(--space-1) 0;
  }

  .monarch-method ol {
    padding-left: 1.25rem;
  }

  .monarch-method li {
    margin: 0.1rem 0;
  }

  .monarch-method code {
    background: var(--surface-base);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
    font-size: 0.92em;
  }

  /* The hint trigger rides inside the <summary> so it sits beside the label
     rather than below the disclosure. `display: inline-flex` on a wrapper —
     not on the summary itself, which would drop the disclosure triangle in
     some engines — keeps the "?" aligned to the text baseline box. */
  .monarch-method .summary-label {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
  }

  .monarch-method .hint {
    color: var(--text-dim);
    font-size: var(--text-xs);
    margin: var(--space-1) 0;
  }

  .legacy-note {
    margin: var(--space-2) 0 0;
    color: var(--text-dim);
    font-size: var(--text-xs);
  }

  /* Stacked, not label-beside-input — that is `Field`'s arrangement, and it is
     now `Field` doing it. The fields were a hand-rolled label + raw input
     whose `--space-2` vertical padding and inherited 1.5 leading made them
     ~8.6px taller than every other input on the page, with no tier min-height
     to bring them back. */
  .login-form {
    display: grid;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }

  .login-actions {
    display: flex;
    justify-content: flex-end;
  }

  .login-status {
    font-size: var(--text-xs);
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-default);
  }

  .login-status[data-kind='ok'] {
    color: var(--status-success-fg);
    border-color: var(--status-success-bg);
  }

  .login-status[data-kind='auth'],
  .login-status[data-kind='other'] {
    color: var(--text-secondary);
    border-color: var(--border-default);
  }

  .login-status[data-kind='mfa'],
  .login-status[data-kind='cloudflare'] {
    color: var(--text-secondary);
    background: var(--surface-base);
  }

  /* A pending code step is progress, not a problem — it reads as info so a
     user isn't told their correct password failed. */
  .login-status[data-kind='challenge'] {
    color: var(--status-info-fg);
    border-color: var(--status-info-bg);
  }
</style>
