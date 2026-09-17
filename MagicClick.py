# is_paused = False

# from pynput import keyboard
# current_keys = set()
#
# def on_press(key):
#     current_keys.add(key)
#     if (keyboard.Key.cmd in current_keys and
#         keyboard.Key.shift in current_keys and
#         key == keyboard.KeyCode.from_char('p')):
#         on_action()
#
# def on_release(key):
#     current_keys.discard(key)
#
# def on_action():
#     global is_paused
#     print("Cmd+Shift+P pressed")
#     is_paused =not is_paused
#
# listener = keyboard.Listener(on_press=on_press, on_release=on_release)
# listener.daemon = True
# listener.start()

from pynput.keyboard import Key, Controller

keyboard = Controller()

import cv2
import numpy as np
import time
import HandTracking as ht
import autopy
import pyautogui

pTime = 0
width, height = pyautogui.size()
frameR = 280

left_pinch_start = None
drag_threshold = 0.5   # seconds before a pinch becomes a drag
is_dragging = False

cam = 1
cap = cv2.VideoCapture(cam)
cap.set(4, width)
cap.set(4, height)

detector = ht.handDetector(maxHands=1, detectionCon=0.8, trackCon=0.8)
screen_width, screen_height = autopy.screen.size()

print("Virtual Mouse Started!")
print("Controls:")
print("- Index finger up: Move cursor")
print("- Index + Middle finger up (pinch): Left Click / Drag")
print("- Index + Middle + Ring finger up: Right Click")
print("- All 5 fingers open: Increase Brightness")
print("- All fingers closed (fist): Decrease Brightness")
print("- Press 'q' to quit")

while True:
    success, img = cap.read()
    if not success:
        print("Failed to capture frame")
        print(f'Camera {cam} was likely disconnected')
        print(f'Attempting to reconnect...')
        cap = cv2.VideoCapture(cam)
        cap.set(4, width)
        cap.set(4, height)
        success, img = cap.read()
        if not success:
            print("Reconnect failed")
            print(f'Press "c" to switch camera')
            print(f'Press "q" to quit')
        else:
            print("Reconnect successful")
            print(f'Camera {cam} is active')
            continue
        continue

    img = cv2.flip(img, 1)
    img = detector.findHands(img)
    # img_overlay = detector.findHands(img)
    lmlist, bbox = detector.findPosition(img, draw=False)

    if len(lmlist) != 0:
        x1, y1 = lmlist[4][1:]
        x2, y2 = lmlist[12][1:]
        x3, y3 = lmlist[16][1:]
        x_thumb, y_thumb = lmlist[4][1:]

        fingers = detector.fingersUp()

        if frameR > 0:
            cv2.rectangle(img, (frameR, frameR),
                         (width - frameR, height - frameR),
                         (255, 0, 255), 2)

        # --- Left click / drag (hold while pinching) ---
        length, img, lineInfo = detector.findDistance(8, 12, img, draw=True, r=5, t=1)
        length2, img2, lineInfo2 = detector.findDistance(5, 9, img, draw=False, r=5, t=1)

        current_time = time.time()

        if fingers[1]==1 and fingers[2]==1 and fingers[3]==0 and fingers[4]==0:
            if length <= length2 + 3:
                if left_pinch_start is None:
                    left_pinch_start = current_time

                pinch_duration = current_time - left_pinch_start

                # After threshold, hold the button for drag/select
                if pinch_duration >= drag_threshold:
                    if not is_dragging:
                        # autopy.mouse.toggle(autopy.mouse.Button.LEFT, True)
                        is_dragging = True
                    cv2.circle(img, (lineInfo[4], lineInfo[5]), 15, (0, 255, 0), cv2.FILLED)
                    cv2.putText(img, "Release to switch slides", (50, 50), cv2.FONT_HERSHEY_PLAIN, 2, (0, 255, 0), 3)

                else:
                    # Visual feedback that a pinch is being held
                    progress = min(pinch_duration / drag_threshold, 1.0)
                    radius = int(5 + 10 * progress)
                    cv2.circle(img, (lineInfo[4], lineInfo[5]), radius, (0, 200, 255), cv2.FILLED)

            else:
                # Release ended
                if is_dragging:
                    keyboard.tap(Key.space)
                    is_dragging = False

                left_pinch_start = None

        current_time = time.time()

        cTime = time.time()
        fps = 1 / (cTime - pTime) if (cTime - pTime) > 0 else 0
        pTime = cTime

        cv2.putText(img, f'FPS: {int(fps)}', (width - 150, 50),
                   cv2.FONT_HERSHEY_PLAIN, 2, (255, 0, 0), 2)

        cv2.imshow("Virtual Mouse", img)

        key = cv2.waitKey(1)

        if key == ord('q'):
            break

        if key == ord('c'):
            if cam >= 5:
                cam = 0
            else:
                cam += 1
            cap = cv2.VideoCapture(cam)
            cap.set(4, width)
            cap.set(4, height)
            print(f'Switching to camera {cam}')

cap.release()
cv2.destroyAllWindows()



print("Virtual Mouse Stopped!")