# Kythera's Shader + Addon

blender addon that slaps Kythera's ZZZ shader on zenless zone zero models n carries all the textures over so u dont have to plug 40 image nodes by hand lmao

works on

- **raw zzz fbx** exports, the ones w `MAT_<Name>_UI` materials n a `Materials` folder full of json
- models already set up w the hoyoverse setup wizard (`ZZZ Shader Body`, `ZZZ Shader Face` etc)

needs blender 5.0 or newer n Kythera's ZZZ Shader blend file (not included, get it from Kythera)

## install

1. download `kythera_shader.py` from [releases](https://github.com/Jideeh1/Kythera-s-Shader/releases)
2. Blender > Edit > Preferences > Add-ons > Install from Disk, pick the file
3. tick it on
4. find it in the 3D view sidebar (N) under the **Kythera** tab

thats it. it updates itself from now on

## how to use

### raw fbx

1. click **Import Kythera's Shader** once n pick the kythera blend. it remembers it forever after this
2. click **Import Raw ZZZ FBX** n pick the character fbx
3. done. body hair weapons eyes face all converted, sun n face stuff set up

### setup wizard models

just click **Import Kythera's Shader**. every mesh in the scene gets converted

### added stuff after converting

**Reapply To New Objects** converts anything new in the scene w the cached shader

## what it actually does

- swaps every zzz material for Kythera's body or face shader
- finds \_D \_M \_A \_N for each material, from the json files if they exist, otherwise by matching texture names
- \_D is sRGB, \_M \_A \_N are Non-Color (they're data maps, not colors)
- gives every face its own lightmap from the json so male n female faces both look right
- adds a **Kythera Sun**. the toon shading n the face shadow both follow it
- adds a **Head Direction** empty parented to the head bone so the face shadow moves w the head
- adds a **Kythera Face Vectors** geo nodes modifier to the face that feeds the sun n head direction to the face shader
- hides hair shadow stencil meshes, they r only useful in game

## Addon Panel

| thing                   | what it do                                                                            |
| ----------------------- | ------------------------------------------------------------------------------------- |
| Import Kythera's Shader | converts the scene. only asks for the blend the first time                            |
| Import Raw ZZZ FBX      | imports an fbx n converts just that                                                   |
| Reapply To New Objects  | converts stuff u added after                                                          |
| shader filename + X     | the cached blend. X forgets it so u can pick a newer one                              |
| Light                   | pick the sun, rotate it, change strength, **Fix Face Lighting** redoes the face setup |
| Tools                   | select unconverted, purge old mats, fix texture color spaces, clear cache             |
| Updates                 | check for updates, install, reload addon                                              |

## Updates

on by default. on startup it checks this repo's releases (every 12h max), downloads the new one n keeps a `.bak` of the old file. restart blender or hit **Reload Add-on** to use it

turn it off or change how often in Preferences > Add-ons > Kythera Shader

## Troubleshooting

- **Face shadow on the wrong side**: the addon assumes ur character faces -Y when converting. rotate the Head Direction empty 180 on Z if u converted it facing somewhere else
- **No shadows**: Set world background color to black or use the camera ray technique to keep a custom background. It's not a nice fix but if there are solutions to this, let me know.
- **Skin looks yellow/weird brightening around light/shadow boundary**: turn off SSS. This will be reworked in the next shader update
- **Face fully lit**: sun is in front of the face, thats normal for this shader. rotate the sun to the side
- **Textures missing on raw fbx**: textures need to be in the same folder as the fbx (or a `Textures` folder next to it)
- **Want a newer kythera blend**: clear cache, import again, pick the new file

## Notes from Kythera
- The shader is actually two shaders: one for the face and one for the body.
  - The body shader allows types of light sources other than Sun (Area, Point, Spotlight). It also allows for multiple lights to affect the body shader, which can get messy quickly so just use light/shadow linking if necessary
  - the face shader strictly follows the sun light direction, meaning a point light will NOT affect the face (you might notice some slight hair shadows but this will be fixed). This will work even if the sun is hidden, so you can definitely use it as just a light direction while keeping it hidden from renders, and use other lights to illuminate the rest of the body.

## publishing a release (note to self)

1. bump `bl_info["version"]` in `kythera_shader.py`
2. make a release w the same tag, `(1, 6, 3)` → `v1.6.3`
3. attach `kythera_shader.py`
4. if the tag n bl_info dont match the updater keeps nagging forever lol

## credits

- shader by **Kythera**
- addon by **Jideeh**
