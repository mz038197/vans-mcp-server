# This service shows the same Key Refusal as the router

A Classroom API Key past its own expiry says「API 金鑰已過期，請至 Portal 重新取得邀請碼」. Before that, this service shows 已在其他電腦兌換, 課堂已關閉, 學生已被停用, or 課程已結束或停用, matching the Revocation List entry kind. When more than one applies, the notice is the first that still blocks a new redeem: disabled student, then the Class, then the closed sitting, then the key ended by a newer one. The list has no reason text and no nickname, email, or personal name.

## Considered Options

- **Show 無效的 API 金鑰 for every cause**: rejected. The student should be able to tell the causes apart, and this service should not invent a second wording.
