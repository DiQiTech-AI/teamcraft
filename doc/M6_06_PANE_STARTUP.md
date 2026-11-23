# M6-06 isolated OpenCode startup check

The retained real-probe handoff was used to start separate OpenCode sessions
for the CEO project directory and the Worker allocation. Both local configs
were checked before launch: the only configured provider was oMLX on literal
loopback HTTP, and the configured model matched the handoff pin. Each process
started with an empty inherited environment, a standalone service and its own
disposable HOME and XDG directories.

Both panes reached the interactive OpenCode screen without sending a prompt.
This confirms startup only. The displayed model name was clipped in the pane,
so the full model and native session binding still require operator-local
confirmation. No model inference, Wake, or end-to-end acceptance is claimed.

## Independent binding check

The tester confirmed both pane working directories and the loopback model
configuration. Herdr reported both panes idle, but neither exposed a native
session ID or `interactive_ready`. The clipped footer also prevented
verification of the full selected model. The binding check remains blocked.
A separate CLI probe reported OpenCode v2.4.0. That version does not establish
which executable the existing panes are running.

Before dispatch, verify the native session and effective model for each pane.
The runner must continue rejecting unavailable session or model evidence.
