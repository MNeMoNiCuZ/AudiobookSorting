I want your help to set up a project, and code it for me.
I'm looking to scan through the files and folders in the /input/ directory, try to figure out what the files are.
The files will be audiobooks, but they will be in different formats and structures.
Some are straight audiobooks in a root folder. Some are sub-folder with different books, some are sorted by author, and some are a series.

Ideally, we would create something that proposes a final output for a detected thing, but I manually have to opt-in to each one as I approve it for now, so I want an interface to do this. It could be a simple python GUI, where I can view the entries, and the proposed output, basically showing me the data we got for it.

A lot of books are in .m4b-format, which contains metadata that I hope we can extract.
Some are in the root folder, some are in subfolders.
Sometimes the folder has a series of .mp3-files (i.e. the chapters of the books, or the book split in parts).

My final desired output format is this:
/Author Name/Series XX - Book Title/[Files in here]

Where XX is the index of the book in the series.

The books can have an image baked into the .m4b, and sometimes as an image in the folder.

The UI for the program should list all the entries in the /input/ folder. Each entry gets its own book, and is presented in a table. It should present all the files in a small text box, listing the sub-directories, files etc. in one column to the left, then our proposed name fields, like Author, Series, Series Index, Book Title, in a cleaned up format (Camel Case etc). With a column for the image.

Each line should then have an APPROVE and REJECT button for me, make them colored. And when I press the button, the status of this entry is saved. Each entry is saved in a combined JSON, which gets updated when I vote. Or we can save it all with a SAVE-button, this may be faster.

I will then figure out what to do with this later, maybe we will rename files, folders and such, but for now I just want to view it.

Can you use an API to make searches for names of the file or folder(s) to figure things out?

Additionally, some folders contain multiple books, so it would look like this perhaps:

/input/The Bladeborn Saga/
Book 1 - The Song of the First Blade.m4b
Book 2 - Ghost of the Shadowfort.m4b
Book 3-An Echo of Titans.m4b
Book 4 -The Winds of War The Bladeborn Saga.m4b

In this case, we have 4 books, likely written by the same author, and a series name. Ideally we should be able to figure things like this out. 
Note: You cannot assume any naming structure. Books won't always be named "Book" like this.

We should extract the data in this priority:

#1 metadata, either m4b or mp3.
#2 API search.
#3 Query a language model, we can use a groq api call, I have some code we can use for this later, for now just make placeholder functionality for this.
#4 search engine search? Maybe use a duckduckgo python library?
#5 Make a guess based on reg expression, let's write some formulas to try and figure it out.

API search:
I want us to query an online book api until we find the right data. Please add a new file in /scripts/ for this. Use a suitable free API for this. When we have searched for an entry using metadata, but we are unable to find ALL the entries for Author, Series, Index, Title (each of them), we need to complement this data.I want us to search using the data we have for each entry.

I want to see where we got the info from, so one column should mention which of the above we got it from.