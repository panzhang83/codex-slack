# Slack Thread Navigation Notes

## Scope

This note records the current findings about jumping from Slack App Home into a specific Slack thread message view.

## Current Conclusion

As of this investigation, there is no documented, stable Slack API or deep-link format that lets an App Home button open a specific thread in the native Slack message view.

## What Is Supported

- App Home tabs: `home`, `about`, `messages`
- Deep links for app, channel, user, and file targets
- Message permalinks returned by `chat.getPermalink`

## What Is Not Documented

- A deep link that targets a specific thread from App Home
- A Block Kit action that tells the Slack client to focus a specific thread pane
- A stable native URI format for `channel + thread_ts`

## Practical Implication

- App Home can show useful binding metadata and actions such as `Rename`
- App Home should not promise one-click native thread navigation
- If Slack adds official thread-targeted navigation in the future, this should be revisited

## References

- https://docs.slack.dev/surfaces/app-home/
- https://docs.slack.dev/interactivity/deep-linking/
- https://docs.slack.dev/reference/methods/chat.getPermalink/
- https://stackoverflow.com/questions/76682720/is-it-possible-to-send-a-link-to-a-slack-conversation-directly-to-the-slack-app
