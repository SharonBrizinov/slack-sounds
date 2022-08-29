# slack-sounds: Customize your Slack sound notifications

TL;DR: This is a story of how I felt nostalgic for the old Uh-Oh! ICQ sound and ended up with a quick research on how Slack stores and uses media files. Eventually I ended up developing a simple tool to customize Slack sound notifications. Please note that currently my script edits the `Hummus` notification sound but this can be easily changed if needed.

https://user-images.githubusercontent.com/519424/187280445-04c8d0c4-6c62-426e-b600-f63cad82fa4e.mov

## Instructions (Mac OSX only for now)
1. Change Slack’s notification sound to `Hummus` (we are going to edit it) `Slack-->Preferences-->Notifications-->Select Hummus`
2. run `python3 slack_change_sound.py sound.mp3`
3. Restart Slack

## The Short Story
In essence, Slack does not allow users to customize their sound notifications as they wish but I wanted to choose my own notification sound. After a bit of research I discovered that Slack stores the sound files in multiple locations, but the most important location is a directory with cache files which have a simple yet proprietary binary structure. After a bit of playing I was able to re-create the structure of Slack cache files and I got my desired ICQ sound! I also wrote a generic tool to do this for you.

## The Long Story
#### Fail #1: Denial
So, I wanted to change my Slack notification sound to the classical Uh-Oh! ICQ sound. Sounds easy. I went to Slack Preference --> Notifications but could not find how to choose my own customized sound. Intrigued, I looked online but still could not find how to do this. Looks like Slack limits the user to a few predefined sounds only.

<p align="center">
<img width="559" alt="image" src="https://user-images.githubusercontent.com/519424/187301349-2929a003-1a1c-4ac8-a022-5513a2076532.png">
</p>


OK, maybe Slack doesn't allow you to change the sound to a custom one. Let's find out where the sound files are stored on disk and just replace them manually. For example - let's try to locate `Hummus.mp3` sound and just modify it. `find / -name Hummus.mp3`.

The Slack application is built on the Electron framework. Electron is a free and open-source software framework developed and maintained by GitHub. The framework is designed to create desktop applications using web technologies which are rendered using a flavor of the Chromium browser engine, and a backend using the Node.js runtime environment.

#### Fail #2: Anger
To investigate where resources are stored, I went to Slack installation directory `/Applications/Slack.app` and searched for resource files. I found all the resource files including `Hummus.mp3` Under the `./Contents/Resources` directory. 

<p align="center">
<img width="228" alt="image" src="https://user-images.githubusercontent.com/519424/187301582-d3530ba3-9eb3-4f1c-8a89-27b0afd966de.png">
</p>

Bingo. I changed it to my ICQ sound, restarted Slack, and asked someone to send me a message. Nothing changed. I still hear the annoying `Hummus` sound.


#### Fail #3: Bargaining
I knew that Electron applications store resources in a proprietary archive file called [.asar](https://github.com/electron/asar). So OK, maybe the resource files are extracted every single time from the .asar archive. I unpacked the `app-x64.asar` file using `npx asar extract app-x64.asar app-x64.asar.unpacked` and observed the `Hummus.mp3` file within `app-x64.asar.unpacked` directory. Cool, I modified the file, packed back the .asar archive using `npx asar pack app-x64.asar.unpacked/ app-x64.asar` and restarted Slack. This time Slack did not even open. 

<p align="center">
<img width="277" alt="image" src="https://user-images.githubusercontent.com/519424/187301805-d977b0fa-9887-4d5a-8c10-39076b103ae8.png">
</p>

#### Fail #4: Depression
I looked at [Electron source code](https://github.com/electron/universal/blob/3a30fe989bee57d93b3da7beb9e9bf8ca29639fc/src/asar-utils.ts#L49) and found out that an integrity check was added in the form of SHA-256 hash of the .asar archive header (pure JSON structure after the first DWORD size). No problem, I wrote a simple script to calculate the hash and modified `ElectronAsarIntegrity` within `./Contents/Info.plist`. Restarted Slack again and it still doesn't work. After a quick Google search I remembered that updated Mac OSX applications are signed, so Slack needed to be re-signed again because we modified its files. I used `codesign --sign - --force --deep --preserve-metadata=entitlements /Applications/Slack.app` and now I was finally able to restart Slack successfully! but the sound did not change... Still `Hummus`.

<p align="center">
<img width="1387" alt="image" src="https://user-images.githubusercontent.com/519424/187302314-27ee114c-037b-473b-8e46-187dbb10462c.png">
</p>

Frustrated, I checked all the open file descriptors that Slack is using `lsof -n | grep -i slack` but I could not find any `.mp3` files. Interesting. It means that probably Slack loads this dynamically and uses it in-memory somehow.

<p align="center">
<img width="1700" alt="image" src="https://user-images.githubusercontent.com/519424/187302603-528fcbea-8016-4f6d-82c3-58e62be025d8.png">
</p>


#### Fail #5: Acceptance
At this point I determined that Slack is probably getting the sound files remotely somehow. It was strange because why would Slack request the sound files every single time from a remote server? But it was my only explanation and I decided to research this. A split second before spinning my Burp, I decided to check how the true web application behaves. I opened slack.com and entered my workspace. Went to settings and selected one of the sound files. As expected, a remote sound file resource was requested and retrieved.

<p align="center">
<img width="1057" alt="Slack___Sharon_Brizinov___Claroty" src="https://user-images.githubusercontent.com/519424/187303405-f399fb5e-4e81-481e-8e3c-66388af2f166.png">
</p>

I selected the same sound file again but this time now request was issued. Probably a cache... wait! Maybe something similar happens with our Slack Electron app? but instead of asking the media every time, there will be a cache which stores the last response. This will explain why all of our file modifications didn't work and why we don't see any `.mp3` file loaded in `lsof`.

#### Success!
Using `lsof` I knew where Slack stores it's temporary data (databases, cache files, downloads, index, etc). There are two options - 
1. `~/Library/Application Support/Slack`
2. `~/Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack`

I enter the support directory and saw multiple files and directories. One of them was `Cache`.
<p align="center">
<img width="347" alt="image" src="https://user-images.githubusercontent.com/519424/187304757-88417bae-2c60-41af-9145-68242bef42e3.png">
</p>

Inside `Cache` there's another directory called `Cache_Data` and inside thousands of files with random looking names. probably some kind of hashes and an index file pointing at them.
<p align="center">
<img width="201" alt="image" src="https://user-images.githubusercontent.com/519424/187304922-a93fad7f-3559-46c8-95d5-5ee3f14eea48.png">
</p>

I used `grep` to search for part of the `Hummus.mp3` content and found a match!
<p align="center">
<img width="785" alt="image" src="https://user-images.githubusercontent.com/519424/187305315-875ef442-bdb5-4fc0-bdfd-500aad3f66ed.png">
</p>

After a bit of playing with the files there I understood that `_s` cache files are the ones I needed. I started to explore the binary structure of these cache files and finally wrote my own Slack-cache-file builder. Here are some key elements of a Slack cache file - 

<p align="center">
<img width="914" alt="_Users_sharonbrizinov_Library_Containers_com_tinyspeck_slackmacgap_Data_Library_Application_Support_Slack_Cache_Cache_Data_13f007daa736b6cb_s_-_010_Editor" src="https://user-images.githubusercontent.com/519424/187306609-6ee4c71d-bd03-4617-98ad-783b3f13fde5.png">
</p>

Eventually I wrote a quick-and-dirty Python script which receives a path to a `.mp3` sound file and edits the `Hummus` cache file with the customized file. Great Success.


#### Summary
It was a fun journey into understanding how Slack stores and uses media files. The default media files within the .asar archive are probably there just in case there will be a connectivity issue in the first time Slack is loaded. The real deal are the Slack cache files which are being used heavily by Slack across many different functions, including notification sounds :)
