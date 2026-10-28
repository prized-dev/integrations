# What happens when something fails

Four rules, held by every operation: what an operation made is undone when it fails or is cancelled (cleanup outlives the cancellation, and `close()` waits for it); a path is judged on what the sandbox actually opens, so a link swapped in after the SDK's check changes nothing; an answer that never came (a timeout, a dropped connection, a 5xx) is settled by reading the sandbox, never guessed; and only a resume request the client sends and tracks wakes a sandbox: every request to the edge goes with `Prized-Auto-Resume: false`, so one that lands late wakes nothing. Each row has a test in `tests/` (`failures_*` unless named).

| Operation | Failure | What happens | Test |
| --- | --- | --- | --- |
| create | refused (quota, balance) | raised; nothing made, nothing resent | `test_sandbox::test_create_passes_api_refusals_through` |
| create | answer lost (timeouts, 5xx, an unreadable 201) | resent with the same `Idempotency-Key`; when retries run out, asked once more with that key and the sandbox it names is deleted; raised | `test_create_whose_answer_is_lost_deletes_what_it_made` |
| create | a try's answer lost, then a retry refused (quota, now taken by that try) | the refusal carries `uncertain`, so the sandbox is asked for by key and deleted | `test_create_refused_after_a_lost_try_deletes_what_that_try_made` |
| create | request never arrived | the same: an unknown outcome is asked about | `test_create_that_never_arrived_leaves_nothing` |
| create | sandbox goes to `error` | deleted, raised | `test_sandbox::test_create_deletes_a_sandbox_that_fails_to_boot` |
| create | sandbox never reaches `running` within `start_s` | deleted, raised | `test_create_whose_sandbox_never_runs_deletes_it` |
| create | cancelled after the API accepted it | the request finishes in the background and its sandbox is deleted | `test_create_cancelled_after_the_api_accepted_it_deletes_on_close` |
| create | cancelled while booting | deleted | `test_create_cancelled_while_booting_deletes_it` |
| create | cancelled, and the program ends right after (`asyncio.run`, no `close()`) | the cancelled call waits for its cleanup before it raises, since `asyncio.run` cancels any task still running when `main` returns; so a `wait_for` around it can take longer than its timeout | `test_a_create_cancelled_under_asyncio_run_deletes_its_sandbox_without_close` |
| resume | sandbox cannot be read | raised; never replaced | `test_sandbox::test_resume_does_not_replace_on_a_transient_failure` |
| resume | sandbox deleted or in `error` | replaced and restored from the snapshot; an `error` one is deleted | `test_sandbox::test_resume_replaces_a_sandbox_that_is_gone` |
| resume | deleted while resuming | replaced | `test_sandbox::test_resume_replaces_a_sandbox_deleted_while_it_resumes` |
| resume | refused (balance) | raised; nothing woke, nothing replaced | `test_resume_refused_is_raised_and_nothing_is_replaced` |
| resume | answer lost | sandbox read: `starting` or `running` carries on | `test_resume_whose_answer_is_lost_reads_the_sandbox` |
| resume | woke, then never reaches `running` | paused again, raised | `test_resume_that_wakes_a_sandbox_which_never_runs_pauses_it_again` |
| resume | cancelled after the wake | paused again | `test_resume_cancelled_after_the_wake_pauses_it_again` |
| resume | cancelled while the resume request is on its way | the request runs to its end, then the sandbox is paused | `test_resume_cancelled_in_flight_is_paused_after_it_lands` |
| pause (`pause_on_exit`) | accepted, answer lost (the resend gets `409 sandbox_not_running`) | sandbox read: `pausing` or `paused` is done; never deleted | `test_pause_whose_answer_is_lost_never_deletes` |
| pause | failed, sandbox cannot be read | nothing deleted; `delete()` raises until it can tell | `test_pause_with_an_unknown_outcome_deletes_nothing` |
| pause | refused, sandbox still running | raised; left running, never deleted, since its workspace may be saved nowhere else; the next release tries again | `test_pause_that_did_not_land_keeps_the_sandbox_and_raises` |
| pause | sandbox in `error` | deleted: it can never run again | `test_pause_of_a_sandbox_in_error_deletes_it` |
| delete | refused, or still there | `delete()` raises; the next one tries again | `test_sandbox::test_delete_raises_when_the_sandbox_is_still_there` |
| delete | answer lost | sandbox read: `deleting` or gone is done | `test_delete_whose_answer_is_lost_reads_the_sandbox` |
| delete | cancelled midway | finishes in the background | `test_delete_cancelled_midway_still_deletes` |
| fork | | never called: the SDK's sandbox interface has no fork | |
| exec | answer lost (timeout, 502, a proxy's 503) | never resent; `retryable=None`, since it may have run | `test_exec_answered_by_a_proxy_503_is_not_marked_retryable` |
| exec | answer lost, or one that could not be read | the error says which: it never reached the sandbox, it may or may not have run, or it ran (a 2xx it could not read) | `test_api::test_a_command_whose_connection_drops_never_reads_as_could_not_reach`, `test_api::test_a_2xx_it_could_not_read_says_the_command_ran` |
| exec | proven not run (`429`, `sandbox_not_ready`) | resent | `test_sandbox::test_exec_transport_errors` |
| background process | start answer lost | killed by its directory | `test_start_whose_answer_is_lost_kills_the_process` |
| background process | start lost, kill fails too | still tracked; `pty_terminate_all` kills it | `test_lost_start_whose_kill_fails_is_ended_by_terminate_all` |
| background process | kill arrives before its start | the start runs nothing | `test_a_kill_that_arrives_before_its_start_stops_the_start` |
| background process | input not delivered | `ExecTransportError` (`stdin_not_delivered`), or the exit code if it exited | `test_input_that_cannot_be_delivered_is_an_error` |
| background process | two polls at once | one at a time; each byte once | `test_concurrent_polls_return_each_byte_once` |
| write | link swapped in after the SDK's check | refused; nothing written outside | `test_write_never_follows_a_link_swapped_in_after_the_check` |
| write | directory swapped for a link | refused | `test_write_never_follows_a_directory_swapped_for_a_link` |
| write | hard link to a file outside | replaced, not written through | `test_write_replaces_a_hard_link_instead_of_writing_through_it` |
| write | a path whose file or directory name ends in a newline | refused: the SDK's resolver cuts trailing newlines, so it would approve another name | `test_write_through_a_link_to_a_name_ending_in_a_newline_is_refused`, `test_write_never_lands_in_a_sibling_named_like_the_root_plus_a_newline` |
| write | upload fails, or cancelled | staged parts removed | `test_write_cancelled_midway_leaves_no_staged_parts` |
| write | two writes make the same new directory at once (a manifest's files are written together) | both land: a directory another write made first is walked into | `test_failures_staging::test_writes_racing_to_make_the_same_directory_both_land` |
| write | a staged part swapped for a link, or rewritten, after its upload | refused (`staged_upload_changed`): the bytes written are checked against the upload's SHA-256 before the rename (the rename itself can still be raced, below) | `test_failures_staging::test_write_never_publishes_a_staged_part_swapped_for_a_link` |
| any | a command, file transfer, or cleanup lands after the session paused the sandbox (cancelled while on the wire) | sent with `Prized-Auto-Resume: false`: refused, and the sandbox stays paused | `test_a_command_that_lands_after_the_pause_leaves_the_sandbox_paused`, `test_cleanup_that_arrives_after_release_leaves_the_sandbox_paused` |
| any | the sandbox paused under the session (`auto_pause_min`) | one tracked resume, shared by the requests waiting on it, then each is sent again | `test_a_command_resumes_a_sandbox_paused_under_the_session` |
| any | the session is releasing its sandbox | nothing resumes it; a resume already on its way lands before the pause | `test_a_released_session_never_resumes_its_sandbox`, `test_a_release_waits_for_a_resume_already_on_its_way` |
| read | link swapped to point outside | refused on the open file | `test_read_never_returns_a_file_a_swapped_link_points_out_to` |
| read | the opened file moved, or its path redirected by a link, while it is checked | refused (`moved_while_checked`): the path must name the open file through no link, before and after the check | `test_read_refuses_a_file_moved_out_while_it_is_checked`, `test_read_refuses_a_path_a_link_redirects_while_it_is_checked` |
| read | copy fails (unreadable, disk full) | an error, never a short file | `test_sandbox::test_a_staged_read_that_fails_is_an_error_not_a_short_file` |
| snapshot | links in the workspace | stored as links, never followed | `test_snapshot_stores_links_as_links` |
| snapshot, restore | the workspace root swapped for a link, also to a sibling named like it plus a newline | refused (`workspace_root_is_a_link`); tar works in the root directory it holds | `test_snapshots_refuse_a_workspace_root_swapped_for_a_link`, `test_snapshots_refuse_a_root_linked_to_a_sibling_named_like_it_plus_a_newline` |
| restore | a virtualenv (`python` links to the system's) | restored, and runs | `test_a_virtualenv_comes_back_in_a_new_sandbox` |
| restore | hard links in the workspace | stored as the files they name (`--hard-dereference`), restored | `test_a_snapshot_with_hard_links_restores` |
| restore | a member under one of the archive's links | refused before upload | `test_hydrate_refuses_a_member_under_a_link_of_its_own` |
| restore | a link already in the workspace leading out | refused by GNU tar beneath the root; nothing written outside | `test_hydrate_never_writes_through_a_link_already_in_the_workspace` |

## Known limit, a process in the sandbox racing a write

The write's last check and its rename are two steps, and bash cannot rename relative to a held descriptor. So a process in the sandbox that moves the directory the write holds somewhere else, or renames another file onto the temporary name after its bytes are checked, gets those bytes published there, and the write reports success. Only a process that could already write there itself can do it: commands in a sandbox all run as `prized`.
